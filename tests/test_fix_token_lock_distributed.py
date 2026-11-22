"""Tests for fix_token_lock_distributed (backend-sec).

Token refresh must use a REAL shared lock (lease TTL, owner token,
safe unlock, acquire timeout) and re-read credential_ref/version from
the shared store after acquiring, so two TokenManagers ("processes")
sharing one vault store refresh an account exactly once.
Run from repo root: python -m pytest tests/test_fix_token_lock_distributed.py -q
"""
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.accounts.vault import (
    EncryptedSecretVault, EnvKekProvider, InMemoryDistributedLock,
    InMemoryVaultStore, PostgresAdvisoryLock, RedisDistributedLock,
    TokenManager, TokenRevokedError,
)
from src.contracts import AccountStatus, Platform, PlatformAccount, TokenSet, utcnow

KEK = bytes(range(32))


def _vault(store):
    return EncryptedSecretVault(kek=EnvKekProvider(kek=KEK), store=store)


def _account(ref, account_id="acc-1"):
    import dataclasses
    kwargs = dict(id=account_id, platform=Platform.FACEBOOK,
                  platform_user_id="u1", credential_ref=ref,
                  status=AccountStatus.ACTIVE)
    if "workspace_id" in {f.name for f in dataclasses.fields(PlatformAccount)}:
        kwargs["workspace_id"] = "ws-1"
    return PlatformAccount(**kwargs)


def _expiring_tokens(minutes=10, access="old-access"):
    return TokenSet(access_token=access, refresh_token="old-refresh",
                    expires_at=utcnow() + timedelta(minutes=minutes))


def _fresh_tokens(access="new-access"):
    return TokenSet(access_token=access, refresh_token="new-refresh",
                    expires_at=utcnow() + timedelta(hours=1))


def _setup(minutes=10):
    store = InMemoryVaultStore()
    vault = _vault(store)
    ref = vault.store_token_set(account_id="acc-1", tokens=_expiring_tokens(minutes))
    return store, vault, ref


# ---------------------------------------------------------------- lock backend semantics
def test_inmemory_lock_owner_safe_unlock_and_timeout():
    lock = InMemoryDistributedLock()
    owner = lock.acquire("k", ttl_seconds=5, timeout_seconds=0.5)
    assert owner
    # another contender times out instead of waiting forever
    t0 = time.monotonic()
    assert lock.acquire("k", ttl_seconds=5, timeout_seconds=0.3) is None
    assert time.monotonic() - t0 < 2.0
    # wrong owner cannot unlock someone else's lock
    assert lock.release("k", "not-the-owner") is False
    assert lock.acquire("k", ttl_seconds=5, timeout_seconds=0.1) is None
    # right owner unlocks; key becomes free
    assert lock.release("k", owner) is True
    assert lock.acquire("k", ttl_seconds=5, timeout_seconds=0.1)


def test_inmemory_lock_lease_expires():
    lock = InMemoryDistributedLock()
    assert lock.acquire("k", ttl_seconds=0.2, timeout_seconds=0.1)
    time.sleep(0.3)  # lease lapses without release (holder crashed)
    assert lock.acquire("k", ttl_seconds=5, timeout_seconds=0.5)


class _FakeRedis:
    def __init__(self):
        self.data = {}

    def set(self, key, value, nx=False, px=None):
        if nx and key in self.data:
            return None
        self.data[key] = value
        return True

    def eval(self, script, numkeys, key, owner):
        if self.data.get(key) == owner:
            del self.data[key]
            return 1
        return 0


def test_redis_lock_set_nx_and_owner_unlock():
    client = _FakeRedis()
    lock = RedisDistributedLock(client)
    owner = lock.acquire("k", ttl_seconds=5, timeout_seconds=0.3)
    assert owner and client.data["lock:k"] == owner
    assert lock.acquire("k", ttl_seconds=5, timeout_seconds=0.2) is None
    assert lock.release("k", "someone-else") is False
    assert "lock:k" in client.data
    assert lock.release("k", owner) is True
    assert "lock:k" not in client.data


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params):
        self._conn.calls.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            self._result = (self._conn.try_result,)
        else:
            self._result = (True,)

    def fetchone(self):
        return self._result

    def close(self):
        pass


class _FakeConn:
    def __init__(self, try_result=True):
        self.try_result = try_result
        self.calls = []
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def test_postgres_advisory_lock_roundtrip_and_timeout():
    conns = []

    def factory():
        c = _FakeConn(try_result=True)
        conns.append(c)
        return c

    lock = PostgresAdvisoryLock(factory)
    owner = lock.acquire("k", ttl_seconds=60, timeout_seconds=0.5)
    assert owner
    assert lock.release("k", "not-owner") is False
    assert lock.release("k", owner) is True
    assert conns[0].closed is True  # connection returned after unlock

    busy = PostgresAdvisoryLock(lambda: _FakeConn(try_result=False))
    assert busy.acquire("k", ttl_seconds=60, timeout_seconds=0.2) is None


# ---------------------------------------------------------------- two managers, one store
def test_two_managers_race_refresher_runs_exactly_once():
    store, vault, ref = _setup()
    backend = InMemoryDistributedLock()
    calls = []
    barrier = threading.Barrier(3)

    def refresher(account, tokens):
        calls.append(account.id)
        time.sleep(0.25)  # slow platform refresh while holding the lock
        return _fresh_tokens()

    m1 = TokenManager(vault, refresher, lock_provider=backend,
                      lock_timeout_seconds=5)
    m2 = TokenManager(vault, refresher, lock_provider=backend,
                      lock_timeout_seconds=5)
    acc_a, acc_b = _account(ref), _account(ref)  # two "processes", same stale ref
    results = {}

    def run(name, mgr, acc):
        barrier.wait()
        results[name] = mgr.ensure_fresh(acc)

    t1 = threading.Thread(target=run, args=("a", m1, acc_a))
    t2 = threading.Thread(target=run, args=("b", m2, acc_b))
    t1.start(); t2.start(); barrier.wait()
    t1.join(10); t2.join(10)

    assert len(calls) == 1, "refresh_token rotation race: refresher ran twice"
    for tokens, status in results.values():
        assert tokens.access_token == "new-access"
        assert status == AccountStatus.ACTIVE
    # both accounts converged on the same single rotated ref in the store
    assert acc_a.credential_ref == acc_b.credential_ref != ref
    assert vault.latest_ref("acc-1", "token_set") == acc_a.credential_ref


def test_second_manager_uses_winners_tokens_without_refreshing():
    store, vault, ref = _setup()
    backend = InMemoryDistributedLock()
    calls = []

    def refresher(account, tokens):
        calls.append(1)
        return _fresh_tokens()

    m1 = TokenManager(vault, refresher, lock_provider=backend)
    acc_a = _account(ref)
    tokens_a, status_a = m1.ensure_fresh(acc_a)
    assert status_a == AccountStatus.ACTIVE and len(calls) == 1

    # second manager still holds the OLD (now rotated-away) ref
    m2 = TokenManager(vault, refresher, lock_provider=backend)
    acc_b = _account(ref)
    tokens_b, status_b = m2.ensure_fresh(acc_b)
    assert len(calls) == 1, "must adopt the winner's tokens, not refresh again"
    assert tokens_b.access_token == "new-access"
    assert status_b == AccountStatus.ACTIVE
    assert acc_b.credential_ref == acc_a.credential_ref


def test_lock_timeout_with_valid_token_fails_closed_no_refresh():
    store, vault, ref = _setup(minutes=10)  # needs refresh, not yet expired
    backend = InMemoryDistributedLock()
    holder = backend.acquire("token-refresh:acc-1", ttl_seconds=30,
                             timeout_seconds=1)
    assert holder
    calls = []

    def refresher(account, tokens):
        calls.append(1)
        return _fresh_tokens()

    mgr = TokenManager(vault, refresher, lock_provider=backend,
                       lock_timeout_seconds=0.3)
    tokens, status = mgr.ensure_fresh(_account(ref))
    assert calls == [], "refresher must NOT run without the lock"
    assert status == AccountStatus.TOKEN_EXPIRING  # fail-closed, retry later
    assert tokens.access_token == "old-access"    # untouched, not rotated
    backend.release("token-refresh:acc-1", holder)


def test_lock_timeout_with_expired_token_needs_reauth():
    store, vault, ref = _setup(minutes=-5)  # already expired
    backend = InMemoryDistributedLock()
    holder = backend.acquire("token-refresh:acc-1", ttl_seconds=30,
                             timeout_seconds=1)
    calls = []
    mgr = TokenManager(vault, lambda a, t: calls.append(1) or _fresh_tokens(),
                       lock_provider=backend, lock_timeout_seconds=0.3)
    tokens, status = mgr.ensure_fresh(_account(ref))
    assert calls == []
    assert status == AccountStatus.NEEDS_REAUTH  # expired + no lock = fail-closed
    backend.release("token-refresh:acc-1", holder)


def test_revoked_and_failure_counting_still_fail_closed():
    store, vault, ref = _setup()

    def revoked(account, tokens):
        raise TokenRevokedError("checkpoint")

    mgr = TokenManager(vault, revoked,
                       lock_provider=InMemoryDistributedLock())
    _, status = mgr.ensure_fresh(_account(ref))
    assert status == AccountStatus.NEEDS_REAUTH

    store2, vault2, ref2 = _setup()

    def boom(account, tokens):
        raise RuntimeError("platform 500")

    mgr2 = TokenManager(vault2, boom, max_failures=2,
                        lock_provider=InMemoryDistributedLock())
    acc = _account(ref2)
    assert mgr2.ensure_fresh(acc)[1] == AccountStatus.TOKEN_EXPIRING
    assert mgr2.ensure_fresh(acc)[1] == AccountStatus.NEEDS_REAUTH


def test_default_backend_is_shared_across_managers():
    # No lock_provider injected: two managers in one process must still
    # exclude each other via the process-wide shared default backend.
    store, vault, ref = _setup()
    calls = []

    def refresher(account, tokens):
        calls.append(1)
        time.sleep(0.2)
        return _fresh_tokens()

    m1 = TokenManager(vault, refresher, lock_timeout_seconds=5)
    m2 = TokenManager(vault, refresher, lock_timeout_seconds=5)
    out = {}
    barrier = threading.Barrier(3)

    def run(name, mgr):
        barrier.wait()
        out[name] = mgr.ensure_fresh(_account(ref))

    t1 = threading.Thread(target=run, args=("a", m1))
    t2 = threading.Thread(target=run, args=("b", m2))
    t1.start(); t2.start(); barrier.wait()
    t1.join(10); t2.join(10)
    assert len(calls) == 1
