"""Regression guard for fix_scheduler_compose_no_account_source.

The bug: the compose `scheduler` service had NO account source. The
entrypoint (src/scheduler/__main__.py) reads accounts only from the
JSON file named by SCHEDULER_ACCOUNTS_FILE, but compose never set
that variable and mounted no file — DATABASE_URL/REDIS_URL were
present yet are not used by the entrypoint to load accounts. Every
tick was therefore a logged no-op: a "healthy" stack that never
scheduled a single job.

What this guard locks:

1. Compose wiring — the scheduler service sets
   SCHEDULER_ACCOUNTS_FILE to a path on a volume that is mounted
   into BOTH the api service (account owner/writer) and the
   scheduler (reader), the volume is declared, and the scheduler
   waits for the api to be healthy before its one-time load.
2. Behavior — with an accounts file present (the state the wiring
   produces), `python -m src.scheduler --once` really enqueues jobs
   for a configured account; without one it stays a logged no-op
   (exit 0, enqueued 0, nothing fabricated).

Known remaining half (tracked outside this change, in
src/scheduler/__main__.py + the api's account persistence): the
entrypoint still loads the file once at startup and does not read
the shared Postgres store per tick; the file channel wired here is
the stack-level bridge until that lands.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "docker" / "docker-compose.yml"
SHARED_VOLUME = "scheduler_accounts"
ACCOUNTS_PATH = "/run/accounts/accounts.json"


def _compose():
    """(parsed dict or None, raw text). Parsed when PyYAML is present;
    the text fallback checks the same facts textually."""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ImportError:
        return None, text
    return yaml.safe_load(text), text


def _env_of(service: dict) -> dict:
    env = service.get("environment") or {}
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    out = {}
    for item in env:  # list form "KEY=value"
        key, _, value = str(item).partition("=")
        out[key] = value
    return out


def _mounts_of(service: dict) -> dict:
    """volume name -> mount path, for short-syntax mounts."""
    mounts = {}
    for entry in service.get("volumes") or []:
        name, _, target = str(entry).partition(":")
        mounts[name] = target
    return mounts


def test_compose_wires_shared_account_source():
    data, text = _compose()
    if data is None:
        assert f"SCHEDULER_ACCOUNTS_FILE: {ACCOUNTS_PATH}" in text
        assert text.count(f"- {SHARED_VOLUME}:/run/accounts") == 2, (
            "shared accounts volume must be mounted into api AND scheduler")
        assert re.search(rf"(?m)^  {SHARED_VOLUME}:", text), (
            "shared accounts volume is not declared")
        return
    services = data["services"]
    scheduler = services["scheduler"]
    env = _env_of(scheduler)
    assert env.get("SCHEDULER_ACCOUNTS_FILE") == ACCOUNTS_PATH, (
        "scheduler has no account source again — every tick will be a "
        "silent no-op (the original bug)")
    sched_mounts = _mounts_of(scheduler)
    assert sched_mounts.get(SHARED_VOLUME) == "/run/accounts", (
        "the accounts file's directory is not a mounted volume in scheduler")
    api_mounts = _mounts_of(services["api"])
    assert api_mounts.get(SHARED_VOLUME) == "/run/accounts", (
        "api does not mount the shared accounts volume — the channel "
        "exists on the reader side only, so the file can never appear")
    assert SHARED_VOLUME in (data.get("volumes") or {}), (
        "shared accounts volume is used but not declared")
    depends = scheduler.get("depends_on") or {}
    assert "api" in depends, (
        "scheduler no longer waits for the api (its one-time account "
        "load can race the api's first export)")


def _run_scheduler_once(accounts_file: Path | None, tmp_path: Path):
    env = dict(os.environ)
    if accounts_file is None:
        env.pop("SCHEDULER_ACCOUNTS_FILE", None)
    else:
        env["SCHEDULER_ACCOUNTS_FILE"] = str(accounts_file)
    return subprocess.run(
        [sys.executable, "-m", "src.scheduler", "--once"],
        cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        timeout=120)


def _tick_counts(output: str) -> dict:
    m = re.search(r"tick done: \{([^}]*)\}", output)
    assert m, f"no tick summary in scheduler output:\n{output}"
    return {k: int(v) for k, v in re.findall(r"'(\w+)': (\d+)", m.group(1))}


def test_scheduler_tick_enqueues_for_account_from_shared_file(tmp_path):
    accounts = [{
        "id": "acc-sched-1", "workspace_id": "ws1", "platform": "facebook",
        "platform_user_id": "fb-1", "display_name": "Sched One",
        "status": "active",
        "videos": [{"video_id": "v1", "is_new": True,
                    "published_at": "2026-01-01T00:00:00Z"}],
    }]
    accounts_file = tmp_path / "accounts.json"
    accounts_file.write_text(json.dumps(accounts), encoding="utf-8")
    proc = _run_scheduler_once(accounts_file, tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"scheduler --once failed:\n{output}"
    counts = _tick_counts(output)
    assert counts["accounts"] == 1
    assert counts["errors"] == 0
    assert counts["enqueued"] >= 1, (
        f"account configured via the shared file but nothing enqueued: {counts}")


def test_scheduler_without_accounts_file_is_logged_noop_not_crash(tmp_path):
    proc = _run_scheduler_once(None, tmp_path)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"scheduler --once failed:\n{output}"
    assert "no accounts configured" in output
    counts = _tick_counts(output)
    assert counts["accounts"] == 0 and counts["enqueued"] == 0


if __name__ == "__main__":
    import tempfile
    test_compose_wires_shared_account_source()
    with tempfile.TemporaryDirectory() as d:
        test_scheduler_tick_enqueues_for_account_from_shared_file(Path(d))
    with tempfile.TemporaryDirectory() as d:
        test_scheduler_without_accounts_file_is_logged_noop_not_crash(Path(d))
    print("ALL TESTS PASSED")
