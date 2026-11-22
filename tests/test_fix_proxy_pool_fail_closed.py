"""fix_proxy_pool_fail_closed — proxy pool fail-closed.

Loi cu (da tai hien tren VM truoc khi fix):
1. health_check() khong co checker tra ok=False nhung proxy van ACTIVE
   va van duoc cap.
2. _candidates() tra toan bo pool khi het proxy cung geo nen failover
   len sang quoc gia khac, ghi sai reason='failover_same_geo', va dat
   WARMUP len ProxyInfo dung chung.
3. QUARANTINED khong co duong quay lai: pool teo toi rong.
4. _pick/acquire khong loai proxy da co binding cua account khac nen
   nhieu account chung 1 IP.

Stdlib + pytest only. Chay tu repo root:
pytest -q tests/test_fix_proxy_pool_fail_closed.py
"""
from __future__ import annotations

import random
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import (  # noqa: E402
    ProxyHealth,
    ProxyInfo,
    ProxyKind,
    ProxyStatus,
    utcnow,
)
from src.proxy.proxy_pool_manager import (  # noqa: E402
    ProxyPoolManager,
    ProxyUnavailable,
)


def _proxy(pid: str, country: str, *, kind: ProxyKind = ProxyKind.RESIDENTIAL) -> ProxyInfo:
    return ProxyInfo(id=pid, host="10.0.0.1", port=8080, kind=kind,
                     country=country, status=ProxyStatus.ACTIVE)


def _pool(*proxies, checker=None, **kw) -> ProxyPoolManager:
    m = ProxyPoolManager(health_checker=checker, epsilon=0.0,
                         rng=random.Random(0), **kw)
    for p in proxies:
        m.add_proxy(p)
    return m


def _ok_checker(proxy: ProxyInfo) -> ProxyHealth:
    return ProxyHealth(proxy_id=proxy.id, ok=True, latency_ms=50.0)


def _bad_checker(proxy: ProxyInfo) -> ProxyHealth:
    return ProxyHealth(proxy_id=proxy.id, ok=False, latency_ms=0.0,
                       blacklisted=True, error="blacklisted")


def _expire_circuit(m: ProxyPoolManager, proxy_id: str) -> None:
    m._stats[proxy_id].circuit_open_until = utcnow() - timedelta(seconds=1)


# --- 1. Unverified khong duoc cap -------------------------------------------

def test_health_check_without_checker_quarantines_proxy():
    m = _pool(_proxy("vn-1", "VN"))
    health = m.health_check("vn-1")
    assert health.ok is False
    assert m.get("vn-1").status == ProxyStatus.QUARANTINED
    # Loi cu: acquire van tra vn-1. Ban moi: fail-closed.
    with pytest.raises(ProxyUnavailable):
        m.acquire("acc-1")


def test_health_check_bad_checker_quarantines_immediately():
    m = _pool(_proxy("vn-1", "VN"), checker=_bad_checker)
    health = m.health_check("vn-1")
    assert health.ok is False
    assert m.get("vn-1").status == ProxyStatus.QUARANTINED
    with pytest.raises(ProxyUnavailable):
        m.acquire("acc-1")


def test_health_check_ok_keeps_proxy_usable():
    m = _pool(_proxy("vn-1", "VN"), checker=_ok_checker)
    assert m.health_check("vn-1").ok is True
    assert m.get("vn-1").status == ProxyStatus.ACTIVE
    assert m.acquire("acc-1").id == "vn-1"


def test_require_health_checker_rejects_pool_without_checker():
    with pytest.raises(ValueError):
        ProxyPoolManager(require_health_checker=True)


# --- 2. Failover cung geo that -----------------------------------------------

def test_candidates_same_geo_only_and_failover_raises_when_no_same_geo():
    vn = _proxy("vn-1", "VN")
    us = _proxy("us-1", "US")
    m = _pool(vn, us, checker=_ok_checker)
    m.acquire("acc-1")
    assert m.get("vn-1").id == "vn-1"
    # Giet proxy VN (3 loi -> quarantine).
    for _ in range(3):
        m.report_result(proxy_id="vn-1", ok=False, latency_ms=10)
    assert vn.status == ProxyStatus.QUARANTINED
    # Loi cu: _candidates("VN") tra [us-1], failover len sang US.
    assert m._candidates("VN") == []
    with pytest.raises(ProxyUnavailable):
        m.failover("acc-1")
    # Account khong bi gan len sang US.
    assert m._bindings["acc-1"].proxy_id == "vn-1"


def test_failover_same_geo_does_not_set_shared_warmup():
    vn1 = _proxy("vn-1", "VN")
    vn2 = _proxy("vn-2", "VN")
    m = _pool(vn1, vn2, checker=_ok_checker)
    m.acquire("acc-1")
    for _ in range(3):
        m.report_result(proxy_id="vn-1", ok=False, latency_ms=10)
    replacement = m.failover("acc-1")
    assert replacement.id == "vn-2"
    # Loi cu: replacement.status == WARMUP tren ProxyInfo dung chung.
    assert replacement.status == ProxyStatus.ACTIVE
    # Warm-up nam tren binding cua account nay, khong phai ProxyInfo.
    assert m.is_probation("acc-1") is True
    assert m.probation_proxy("acc-1") == "vn-2"


# --- 3. Quarantine co hoi phuc -------------------------------------------------

def test_quarantined_proxy_reactivates_after_circuit_expires():
    state = {"ok": False}

    def checker(proxy: ProxyInfo) -> ProxyHealth:
        return ProxyHealth(proxy_id=proxy.id, ok=state["ok"], latency_ms=50.0)

    vn = _proxy("vn-1", "VN")
    m = _pool(vn, checker=checker)
    m.health_check("vn-1")  # bad -> quarantine
    assert vn.status == ProxyStatus.QUARANTINED
    with pytest.raises(ProxyUnavailable):
        m.acquire("acc-1")
    # Circuit chua het: khong probe, khong phuc hoi.
    assert m._candidates("VN") == []
    # Het circuit + checker ok -> probe phuc hoi.
    state["ok"] = True
    _expire_circuit(m, "vn-1")
    got = m.acquire("acc-1")
    assert got.id == "vn-1"
    assert vn.status == ProxyStatus.ACTIVE
    # Account nhan proxy vua phuc hoi thi o probation (per-binding).
    assert m.is_probation("acc-1") is True
    # 2 request ok -> het probation.
    m.report_result(proxy_id="vn-1", ok=True, latency_ms=50, account_id="acc-1")
    assert m.is_probation("acc-1") is True
    m.report_result(proxy_id="vn-1", ok=True, latency_ms=50, account_id="acc-1")
    assert m.is_probation("acc-1") is False


def test_quarantined_proxy_without_checker_never_reactivates():
    m = _pool(_proxy("vn-1", "VN"))
    m.health_check("vn-1")  # no checker -> quarantine
    _expire_circuit(m, "vn-1")
    assert m._candidates("VN") == []
    assert m.reactivate_due_proxies() == []


def test_reactivate_due_proxies_recovers_expired_quarantine():
    state = {"ok": True}
    vn = _proxy("vn-1", "VN")
    m = _pool(vn, checker=lambda p: ProxyHealth(proxy_id=p.id, ok=state["ok"], latency_ms=50.0))
    for _ in range(3):
        m.report_result(proxy_id="vn-1", ok=False, latency_ms=10)
    assert vn.status == ProxyStatus.QUARANTINED
    _expire_circuit(m, "vn-1")
    assert m.reactivate_due_proxies() == ["vn-1"]
    assert vn.status == ProxyStatus.ACTIVE
    assert m.acquire("acc-1").id == "vn-1"


# --- 4. Cap phat doc quyen mac dinh -------------------------------------------

def test_exclusive_by_default_two_accounts_get_different_proxies():
    m = _pool(_proxy("vn-1", "VN"), _proxy("vn-2", "VN"), checker=_ok_checker)
    assert m.acquire("acc-1").id in ("vn-1", "vn-2")
    first = m._bindings["acc-1"].proxy_id
    second = m.acquire("acc-2")
    assert second.id != first
    assert m.accounts_sharing(first) == ["acc-1"]
    assert m.accounts_sharing(second.id) == ["acc-2"]


def test_exclusive_by_default_single_proxy_second_account_gets_unavailable():
    m = _pool(_proxy("vn-1", "VN"), checker=_ok_checker)
    assert m.acquire("acc-1").id == "vn-1"
    # Loi cu: acc-2 cung duoc cap vn-1 (chung 1 IP).
    with pytest.raises(ProxyUnavailable):
        m.acquire("acc-2")


def test_explicit_share_declares_max_and_shows_sharing_and_low_concurrency():
    m = _pool(_proxy("vn-1", "VN"), checker=_ok_checker,
              max_accounts_per_proxy={ProxyKind.RESIDENTIAL: 2})
    assert m.acquire("acc-1").id == "vn-1"
    assert m.acquire("acc-2").id == "vn-1"
    assert sorted(m.accounts_sharing("vn-1")) == ["acc-1", "acc-2"]
    assert m.sharing_count("vn-1") == 2
    # Proxy da share thi concurrency ha xuong 1.
    assert m.effective_proxy_concurrency("vn-1", default=2) == 1
    # Account thu 3 vuot gioi han share da khai bao.
    with pytest.raises(ProxyUnavailable):
        m.acquire("acc-3")
