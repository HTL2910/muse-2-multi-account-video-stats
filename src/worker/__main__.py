"""Worker loop entrypoint — ``python -m src.worker`` (Dockerfile.worker CMD).

fix_docker_compose_companions: the worker service has no compose
``command:``, so the image CMD must be a real loop, not an empty shell.

The loop is the scheduler's own worker API: ``acquire_job`` (quota +
per-account lock + lease, all enforced inside TaskSchedulerImpl) ->
executor(job) -> ``finish_job`` (success, or retry-with-backoff /
dead-letter on failure). The executor is pluggable via the env var
``WORKER_EXECUTOR`` (dotted path ``module:function`` or
``module.function`` to a callable taking a CollectJob and returning
bool) because the adapter/pipeline wiring lives with the API/store
owners, not in this packaging fix.

Fail-closed about work it cannot do: with NO executor configured the
worker does NOT acquire jobs at all — it logs one loud warning and
idles until SIGTERM. Acquiring a job it cannot execute would burn its
retries into the dead-letter queue; idling loses nothing.

Graceful shutdown: SIGTERM/SIGINT finish the in-flight job, take no
new job, exit 0 (compose stop_grace_period 60s > job lease handling).
"""
from __future__ import annotations

import argparse
import importlib
import logging
import os
import signal
import sys
import time
from typing import Callable, Optional

from src.contracts import Platform
from src.scheduler.task_scheduler import TaskSchedulerImpl

log = logging.getLogger("worker")

_stop = False


def _handle_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    log.info("signal %s received: finishing in-flight job, then stopping", signum)


def load_executor(spec: Optional[str]) -> Optional[Callable]:
    """Resolve WORKER_EXECUTOR='module:function' (or module.function)."""
    if not spec:
        return None
    module_name, _, attr = spec.replace(":", ".").rpartition(".")
    if not module_name or not attr:
        raise ValueError(f"WORKER_EXECUTOR must be 'module:function', got {spec!r}")
    return getattr(importlib.import_module(module_name), attr)


def run_loop(scheduler: TaskSchedulerImpl, executor: Optional[Callable], *,
             worker_id: str, poll_seconds: float = 1.0, once: bool = False) -> int:
    """Acquire/execute/finish until stopped. Returns jobs processed."""
    processed = 0
    while not _stop:
        job = None
        if executor is not None:
            for platform in Platform:
                job = scheduler.acquire_job(worker_id=worker_id, platform=platform)
                if job is not None:
                    break
        if job is None:
            if once:
                break
            time.sleep(poll_seconds)
            continue
        try:
            ok = bool(executor(job))
            scheduler.finish_job(job, ok=ok, error="" if ok else "executor returned False")
        except Exception as exc:  # executor blew up: retry/DLQ via finish_job
            scheduler.finish_job(job, ok=False, error=f"{type(exc).__name__}: {exc}")
        processed += 1
        if once:
            break
    return processed


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="src.worker")
    parser.add_argument("--once", action="store_true",
                        help="one poll iteration and exit (smoke test)")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--worker-id", default=os.environ.get("HOSTNAME", "worker-1"))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    executor = load_executor(os.environ.get("WORKER_EXECUTOR"))
    if executor is None:
        log.warning("WORKER_EXECUTOR not configured: idling without "
                    "acquiring jobs (fail-closed — no job is consumed "
                    "that this worker cannot execute)")
    scheduler = TaskSchedulerImpl(workers=[args.worker_id])
    processed = run_loop(scheduler, executor, worker_id=args.worker_id,
                         poll_seconds=args.poll_seconds, once=args.once)
    log.info("worker stopped after %d job(s)", processed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
