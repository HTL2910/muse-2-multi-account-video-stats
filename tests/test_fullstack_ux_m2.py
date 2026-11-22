"""Tests for expert/fullstack-ux-m2 (approach_04/08/12/16/20).

fix_test_portability_stub_contracts:
- No hardcoded author-machine path (the old absolute /tmp sys.path entry
  is gone). The repo root is derived from this file's location, exactly like
  tests/test_systems_arch_m2.py, so the suite runs from any checkout/OS.
- No wildcard imports: every name is imported explicitly, so a renamed or
  missing contract symbol fails loudly at import time instead of silently.
- No stub contracts: the suite imports the REAL ``src/contracts.py`` of this
  repo and ``test_contracts_are_real_and_locked`` proves it (module file is
  the repo's own file, CONTRACTS_VERSION is locked, PlatformAdapter carries
  the mandatory signatures).
Run from repo root:  python -m pytest tests/test_fullstack_ux_m2.py -q
"""
import inspect
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.contracts as contracts
from src.contracts import (
    CONTRACTS_VERSION,
    DataQuality,
    Platform,
    PlatformAccount,
    PlatformAdapter,
    ProxyInfo,
    ProxyStatus,
    Role,
    UnifiedVideoMetric,
    Video,
)
from src.ingestion.pipeline import (
    InMemoryMetricsStore,
    IngestionPipeline,
    normalize_raw_metric,
)
from src.proxy.proxy_pool_manager import (
    ProxyPoolManager,
    ProxyUnavailable,
    parse_proxy_line,
)
from src.security.rbac_audit import (
    AuditChain,
    LoginRateLimiter,
    Permission,
    PermissionDenied,
    SessionManager,
    _totp_at,
    generate_totp_secret,
    has_permission,
    hash_password,
    require_permission,
    verify_password,
    verify_totp,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


# --- Contract lock: real contracts, locked version, adapter signatures ------
def test_contracts_are_real_and_locked():
    # The contracts module actually imported is THIS repo's src/contracts.py,
    # not a stub copied into a temp dir / site-packages / author machine.
    assert Path(contracts.__file__).resolve() == (REPO_ROOT / "src" / "contracts.py").resolve()
    assert CONTRACTS_VERSION == "1.0.0"
    # PlatformAdapter is a Protocol: all five mandatory methods must exist
    # with the locked parameter names from contracts v1.0.0.
    expected = {
        "authenticate": ["self", "account"],
        "check_alive": ["self", "account"],
        "list_videos": ["self", "account", "limit"],
        "get_video_stats": ["self", "account", "video"],
        "refresh_token": ["self", "account", "tokens"],
    }
    for method, params in expected.items():
        fn = getattr(PlatformAdapter, method, None)
        assert fn is not None, f"PlatformAdapter.{method} missing from contracts"
        assert list(inspect.signature(fn).parameters) == params, (
            f"PlatformAdapter.{method} signature drifted from contracts v1.0.0")
    # list_videos keeps its locked keyword-only default.
    limit_param = inspect.signature(PlatformAdapter.list_videos).parameters["limit"]
    assert limit_param.default == 50
    assert limit_param.kind is inspect.Parameter.KEYWORD_ONLY


def test_no_hardcoded_author_path_in_this_file():
    source = Path(__file__).read_text(encoding="utf-8")
    # Fragments are concatenated so this guard does not match its own source.
    assert ("/tmp" + "/m2") not in source
    assert ("import " + "*") not in source


# --- approach_04: proxy pool -------------------------------------------------
def test_proxy_parse_and_sticky_quarantine_failover():
    info, pw = parse_proxy_line("1.2.3.4:8080:user:pass", country="VN")
    assert pw == "pass" and info.port == 8080 and info.username == "user"
    pm = ProxyPoolManager(epsilon=0.0)
    pm.add_proxy(ProxyInfo(id="p1", host="1.1.1.1", port=80, country="VN"))
    pm.add_proxy(ProxyInfo(id="p2", host="2.2.2.2", port=80, country="VN"))
    got = pm.acquire("acc1")
    assert pm.acquire("acc1").id == got.id  # sticky
    for _ in range(3):
        pm.report_result(proxy_id=got.id, ok=False, latency_ms=10)
    assert pm.get(got.id).status == ProxyStatus.QUARANTINED
    nxt = pm.acquire("acc1")
    assert nxt.id != got.id and nxt.country == "VN"  # failover same geo
    pm2 = ProxyPoolManager()
    try:
        pm2.acquire("nobody")
        assert False, "empty pool must fail closed"
    except ProxyUnavailable:
        pass  # fail-closed


# --- approach_08: ingestion pipeline ------------------------------------------
class FakeAdapter:
    def __init__(self, metrics):
        self.metrics = metrics

    def list_videos(self, account, *, limit=50):
        return [Video(id="v1", account_id=account.id, platform=account.platform,
                      platform_video_id="pv1")]

    def get_video_stats(self, account, video):
        return self.metrics


def _acc():
    return PlatformAccount(id="a1", workspace_id="w1", platform=Platform.YOUTUBE,
                           platform_user_id="u1")


def _metric(views):
    return UnifiedVideoMetric(video_id="v1", account_id="a1",
                              platform=Platform.YOUTUBE, platform_video_id="pv1",
                              views=views, likes=5, comments=1)


def test_pipeline_dedupe_negative_and_spike():
    acc = _acc()
    good = _metric(views=100)
    store = InMemoryMetricsStore()
    pipe = IngestionPipeline(FakeAdapter(good), store)
    r1 = pipe.run(acc)
    assert r1.metrics_written == 1
    r2 = pipe.run(acc)
    assert r2.duplicates_skipped == 1 and len(store) == 1

    # Negative counter -> NEEDS_REVIEW, never stored.
    bad = _metric(views=-5)
    r3 = IngestionPipeline(FakeAdapter(bad), InMemoryMetricsStore()).run(acc)
    assert r3.needs_review == 1 and r3.metrics_written == 0

    # REAL spike case (previously missing): against the stored baseline of
    # 100 views, a jump to 2,000,000 views is >10x the previous snapshot AND
    # >100_000 views in one step, so the pipeline must flag NEEDS_REVIEW and
    # write nothing — the good baseline stays untouched in the store.
    spike = _metric(views=2_000_000)
    r4 = IngestionPipeline(FakeAdapter(spike), store).run(acc)
    assert r4.needs_review == 1
    assert r4.metrics_written == 0
    assert len(store) == 1
    assert store.get_latest(video_id="v1").views == 100

    # A plausible in-range growth (100 -> 250) is NOT a spike and is accepted
    # on a fresh store, proving the spike gate is not just rejecting all runs.
    store2 = InMemoryMetricsStore()
    IngestionPipeline(FakeAdapter(_metric(views=100)), store2).run(acc)
    r5 = IngestionPipeline(FakeAdapter(_metric(views=250)), store2).run(acc)
    assert r5.needs_review == 0

    raw = normalize_raw_metric(
        {"viewCount": "42", "likeCount": 3},
        video=Video(id="v1", account_id="a1", platform=Platform.YOUTUBE,
                    platform_video_id="pv1"),
        account=acc)
    assert raw.views == 42 and raw.quality == DataQuality.PARTIAL  # comments missing -> partial, not guessed


# --- approach_16: RBAC / TOTP / audit / session --------------------------------
def test_rbac_totp_audit_session():
    assert has_permission(Role.VIEWER, Permission.ACCOUNT_VIEW)
    assert not has_permission(Role.VIEWER, Permission.ACCOUNT_DELETE)
    try:
        require_permission(Role.ADMIN, Permission.ACCOUNT_VIEW,
                           workspace_id="w1", resource_workspace_id="w2")
        assert False, "cross-workspace access must be denied (RLS)"
    except PermissionDenied:
        pass
    h = hash_password("s3cret")
    assert verify_password("s3cret", h) and not verify_password("wrong", h)
    secret = generate_totp_secret()
    assert verify_totp(secret, _totp_at(secret, int(time.time() // 30)))
    chain = AuditChain()
    chain.append(workspace_id="w1", actor_id="u1", action="proxy.assign",
                 target_type="proxy", target_id="p1", detail={"password": "x"})
    assert chain.entries()[0].detail["password"] == "***" and chain.verify_chain()
    chain._entries[0].detail["tampered"] = 1
    assert not chain.verify_chain()
    sm = SessionManager()
    s = sm.create(user_id="u1", workspace_id="w1", role=Role.ADMIN)
    assert sm.validate(s.token)
    sm.revoke(s.token)
    assert sm.validate(s.token) is None
    lim = LoginRateLimiter(max_failures=2)
    lim.record_failure("a@x", "1.1.1.1")
    assert lim.record_failure("a@x", "1.1.1.1") and lim.is_locked("a@x", "1.1.1.1")


if __name__ == "__main__":
    test_contracts_are_real_and_locked()
    test_no_hardcoded_author_path_in_this_file()
    test_proxy_parse_and_sticky_quarantine_failover()
    test_pipeline_dedupe_negative_and_spike()
    test_rbac_totp_audit_session()
    print("ALL TESTS PASSED")
