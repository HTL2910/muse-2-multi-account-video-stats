"""Scheduler beat entrypoint — ``python -m src.scheduler --beat``.

fix_docker_compose_companions: compose's scheduler service runs exactly
this command, so it must be a REAL entrypoint (the old setup called the
``task_scheduler`` module directly, which has no ``__main__`` block and
died instantly).

What a tick does, per configured account:
  1. ``scheduler.register_account(account)`` — the account's real status
     becomes known to the scheduler (unknown status = fail-closed).
  2. ``schedule_jobs_for_account(account, videos=...)`` builds the
     SYNC_VIDEOS job plus one FETCH_STATS job per known video.
  3. ``scheduler.enqueue(job)`` for each job — the scheduler's own
     fail-closed gate decides (a BANNED account's jobs come back
     REJECTED and are only counted/logged, never forced in).

Account source: the in-memory scheduler has no store of its own, so the
accounts for this process come from a JSON file named by the env var
``SCHEDULER_ACCOUNTS_FILE``: a list of objects with PlatformAccount
fields plus an optional ``videos`` list ([{video_id, is_new,
published_at}]). Without the file the tick is a logged no-op — the
process stays up and healthy instead of crash-looping, and no job is
ever fabricated for an account nobody configured.

Graceful shutdown: SIGTERM/SIGINT set a stop flag; the current tick
finishes, no new tick starts, the process exits 0 (compose gives the
scheduler stop_grace_period 30s, far above one tick).
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.contracts import AccountStatus, Platform, PlatformAccount
from src.scheduler.task_scheduler import TaskSchedulerImpl, schedule_jobs_for_account

log = logging.getLogger("scheduler")

_stop = False


def _handle_signal(signum, _frame) -> None:
    global _stop
    _stop = True
    log.info("signal %s received: finishing current tick, then stopping", signum)


def load_accounts(path: Optional[str]) -> list[tuple[PlatformAccount, list[dict[str, Any]]]]:
    """Read the accounts JSON file -> [(account, videos)].

    Missing/unset path -> []. Unknown keys in a row are ignored except
    ``videos``, so the file may carry UI metadata without breaking the
    entrypoint; platform/status strings are converted to the contract
    enums, so a bad value fails loudly at startup, not mid-tick.
    """
    if not path:
        return []
    rows = json.loads(Path(path).read_text(encoding="utf-8"))
    fields = {f.name for f in dataclasses.fields(PlatformAccount)}
    entries: list[tuple[PlatformAccount, list[dict[str, Any]]]] = []
    for row in rows:
        videos = list(row.get("videos") or [])
        kwargs = {k: v for k, v in row.items() if k in fields}
        if "platform" in kwargs:
            kwargs["platform"] = Platform(kwargs["platform"])
        if "status" in kwargs:
            kwargs["status"] = AccountStatus(kwargs["status"])
        entries.append((PlatformAccount(**kwargs), videos))
    return entries


def tick(scheduler: TaskSchedulerImpl,
         entries: list[tuple[PlatformAccount, list[dict[str, Any]]]],
         *, now: Optional[datetime] = None) -> dict[str, int]:
    """One beat: schedule + enqueue for every account. Never raises for
    a single bad account — the beat must survive one poisoned account."""
    counts = {"accounts": 0, "enqueued": 0, "rejected": 0, "errors": 0}
    for account, videos in entries:
        counts["accounts"] += 1
        try:
            scheduler.register_account(account)
            for job in schedule_jobs_for_account(account, videos=videos, now=now):
                result = scheduler.enqueue(job)
                if result.status.value == "rejected":
                    counts["rejected"] += 1
                else:
                    counts["enqueued"] += 1
        except Exception:  # one account must not kill the beat
            counts["errors"] += 1
            log.exception("tick failed for account %s", getattr(account, "id", "?"))
    log.info("tick done: %s", counts)
    return counts


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="src.scheduler")
    parser.add_argument("--beat", action="store_true",
                        help="run the tick loop (what compose uses)")
    parser.add_argument("--once", action="store_true",
                        help="run a single tick and exit (smoke test / cron)")
    parser.add_argument("--interval", type=float, default=None,
                        help="seconds between ticks (default: env "
                             "SCHEDULER_INTERVAL_SECONDS or 60)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    interval = args.interval
    if interval is None:
        interval = float(os.environ.get("SCHEDULER_INTERVAL_SECONDS", "60"))

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    entries = load_accounts(os.environ.get("SCHEDULER_ACCOUNTS_FILE"))
    if not entries:
        log.warning("no accounts configured (SCHEDULER_ACCOUNTS_FILE unset "
                    "or empty): ticks will be no-ops")
    scheduler = TaskSchedulerImpl()

    while not _stop:
        tick(scheduler, entries)
        if args.once or not args.beat:
            break
        # Sleep in short slices so SIGTERM is honoured within ~1s, not
        # only after the full interval.
        deadline = time.monotonic() + interval
        while not _stop and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
    log.info("scheduler stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
