"""Regression tests for fix_audit_chain_thread_safety.

Old AuditChain had two holes:
1. append() read prev_hash from the last entry and appended WITHOUT a
   lock — two concurrent writers could both chain onto the same
   prev_hash and fork the chain;
2. entries() returned the stored AuditLogEntry objects themselves, so
   any caller could rewrite history in place and "append-only" was only
   a convention.

Fix: append is one critical section under a threading.Lock (production:
DB transaction + advisory lock per workspace), append()/entries()
return deep copies, and the class documents itself as TAMPER-EVIDENT
(detects bypass edits via verify_chain) rather than tamper-proof;
append-only at rest is enforced in PostgreSQL by the trigger in
AUDIT_APPEND_ONLY_SQL.

Run from repo root:  python -m pytest tests/test_fix_audit_chain_thread_safety.py -q
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.security.rbac_audit import AUDIT_APPEND_ONLY_SQL, AuditChain


def _append(chain: AuditChain, i: int, ws: str = "w1"):
    return chain.append(workspace_id=ws, actor_id=f"user{i % 4}",
                        action="account.create", target_type="account",
                        target_id=f"acc{i}", detail={"n": i, "api_token": "should-redact"})


def test_concurrent_appends_never_fork_the_chain():
    chain = AuditChain()
    n_threads, per_thread = 8, 50
    barrier = threading.Barrier(n_threads)

    def worker(base: int):
        barrier.wait()  # release all writers at once to maximise contention
        for i in range(per_thread):
            _append(chain, base + i)

    threads = [threading.Thread(target=worker, args=(t * per_thread,))
               for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    entries = chain.entries()
    assert len(entries) == n_threads * per_thread
    # Exactly one GENESIS head and every other prev_hash unique + linked:
    prev_hashes = [e.prev_hash for e in entries]
    assert prev_hashes[0] == "GENESIS" and prev_hashes.count("GENESIS") == 1
    assert len(set(prev_hashes)) == len(prev_hashes), "chain forked: duplicate prev_hash"
    for prev_entry, entry in zip(entries, entries[1:]):
        assert entry.prev_hash == prev_entry.entry_hash
    assert chain.verify_chain() is True


def test_entries_returns_deep_copies_not_live_objects():
    chain = AuditChain()
    _append(chain, 1)
    _append(chain, 2)
    leaked = chain.entries()
    leaked[0].action = "account.delete"          # rewrite history via the copy
    leaked[0].detail["n"] = 999999
    leaked[0].entry_hash = "0" * 64
    leaked.append(leaked[0])                      # even the list itself is a copy
    fresh = chain.entries()
    assert len(fresh) == 2
    assert fresh[0].action == "account.create" and fresh[0].detail["n"] == 1
    assert chain.verify_chain() is True


def test_append_return_value_is_also_a_copy():
    chain = AuditChain()
    returned = _append(chain, 1)
    returned.action = "tampered"
    returned.detail["n"] = -1
    returned.entry_hash = "f" * 64
    assert chain.entries()[0].action == "account.create"
    assert chain.verify_chain() is True


def test_chain_is_tamper_evident_against_bypass_edits():
    # Tamper-EVIDENT semantics: an attacker who bypasses the API and edits
    # the stored record directly (memory here; a rogue SQL UPDATE in prod,
    # which AUDIT_APPEND_ONLY_SQL forbids) is DETECTED by verify_chain.
    chain = AuditChain()
    _append(chain, 1)
    _append(chain, 2)
    assert chain.verify_chain() is True
    chain._entries[0].detail["n"] = 12345  # bypass edit, not via any public API
    assert chain.verify_chain() is False


def test_workspace_filter_and_redaction_still_work_with_copies():
    chain = AuditChain()
    _append(chain, 1, ws="w1")
    _append(chain, 2, ws="w2")
    w1 = chain.entries(workspace_id="w1")
    assert len(w1) == 1 and w1[0].workspace_id == "w1"
    assert w1[0].detail["api_token"] == "***"  # secrets still redacted before hashing
    assert chain.verify_chain() is True


def test_postgres_append_only_trigger_is_specified():
    sql = AUDIT_APPEND_ONLY_SQL.upper()
    assert "CREATE TRIGGER" in sql and "BEFORE UPDATE OR DELETE" in sql
    assert "RAISE EXCEPTION" in sql


if __name__ == "__main__":
    test_concurrent_appends_never_fork_the_chain()
    test_entries_returns_deep_copies_not_live_objects()
    test_append_return_value_is_also_a_copy()
    test_chain_is_tamper_evident_against_bypass_edits()
    test_workspace_filter_and_redaction_still_work_with_copies()
    test_postgres_append_only_trigger_is_specified()
    print("ALL TESTS PASSED")
