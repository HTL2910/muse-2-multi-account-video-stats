"""Tests for fix_sqlite_manual_queue_connection_leak (backend-sec).

Root cause: SQLiteManualQueue used ``with self._connect() as conn:``
where _connect() returned a raw sqlite3.Connection — a Connection's
__exit__ commits/rolls back but NEVER closes, so every operation
leaked one file descriptor (reclaimed only by cyclic GC, at no
deterministic time). On Windows the open handle blocks unlinking
manual_queue.sqlite3 (WinError 32), failing the 3 profile tests of
test_backend_sec_m2.py at TemporaryDirectory teardown.

Fix: _connect() is now a contextmanager that commits on success,
rolls back on exception and ALWAYS closes in finally. These tests
prove (a) no FD to the db file survives any operation — without
calling gc.collect(), which would mask the leak — (b) an exception
inside a session still closes AND rolls back, and (c) queue
semantics (dedupe, claim/fail/complete, persistence) are unchanged.
Run from repo root: python -m pytest tests/test_fix_sqlite_manual_queue_connection_leak.py -q
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.browser.profile_manager import SQLiteManualQueue


def _fds_to(path: Path) -> int:
    """Count this process's open FDs pointing at ``path`` (Linux /proc)."""
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.exists():
        pytest.skip("/proc/self/fd unavailable on this platform")
    target = str(path)
    count = 0
    for fd in fd_dir.iterdir():
        try:
            if os.readlink(fd) == target:
                count += 1
        except OSError:
            continue
    return count


def _exercise(q: SQLiteManualQueue) -> str:
    item_id = q.enqueue({"account_id": "acc-1", "kind": "checkpoint",
                         "url": "https://example.test/x"})
    q.enqueue({"account_id": "acc-1", "kind": "checkpoint"})  # dedupe hit
    q.enqueue({"account_id": "acc-2", "kind": "captcha"})
    q.list_items()
    q.get(item_id)
    claimed = q.claim(item_id)
    assert claimed is not None and claimed["attempts"] == 1
    q.fail(item_id, "still blocked")
    q.claim(item_id)
    assert q.complete(item_id) is True
    q.list_items(statuses=("PENDING", "IN_PROGRESS", "DONE", "FAILED"))
    return item_id


def test_no_fd_leak_after_operations(tmp_path):
    db = tmp_path / "manual_queue.sqlite3"
    q = SQLiteManualQueue(db)
    assert _fds_to(db) == 0, "even __init__ (CREATE TABLE) must not leak an FD"
    _exercise(q)
    assert _fds_to(db) == 0, "operations leaked FDs to the queue db"
    _exercise(q)
    assert _fds_to(db) == 0, "FD count must stay at zero across rounds"


def test_exception_inside_session_rolls_back_and_closes(tmp_path):
    db = tmp_path / "manual_queue.sqlite3"
    q = SQLiteManualQueue(db)
    with pytest.raises(RuntimeError):
        with q._connect() as conn:
            conn.execute(
                "INSERT INTO manual_queue (id, account_id, kind, created_at,"
                " updated_at) VALUES ('mq-x', 'acc-9', 'checkpoint', 't', 't')")
            raise RuntimeError("boom mid-transaction")
    assert _fds_to(db) == 0, "exception path must still close the connection"
    assert q.get("mq-x") is None, "exception path must roll back, not commit"


def test_db_file_removable_right_after_operations(tmp_path):
    # The Windows failure mode: an open handle blocks unlink. On Linux
    # unlink succeeds even with leaked handles, so this is a smoke
    # check only — the FD-count tests above are the authoritative ones.
    db = tmp_path / "manual_queue.sqlite3"
    q = SQLiteManualQueue(db)
    _exercise(q)
    os.remove(db)
    assert not db.exists()


def test_queue_semantics_unchanged(tmp_path):
    db = tmp_path / "manual_queue.sqlite3"
    q = SQLiteManualQueue(db)
    first = q.enqueue({"account_id": "a1", "kind": "checkpoint"})
    again = q.enqueue({"account_id": "a1", "kind": "checkpoint"})
    assert again == first, "open duplicate (account, kind) must dedupe"
    assert len(q.list_items()) == 1
    # fail cycles back to PENDING until attempts reach max_attempts
    q.claim(first)
    assert q.fail(first, "e1") == "PENDING"
    q.claim(first)
    assert q.fail(first, "e2") == "PENDING"
    q.claim(first)
    assert q.fail(first, "e3") == "FAILED"
    assert q.get(first)["status"] == "FAILED"
    # persistence: a second instance over the same file sees the item
    q2 = SQLiteManualQueue(db)
    assert q2.get(first)["status"] == "FAILED"
    assert _fds_to(db) == 0
