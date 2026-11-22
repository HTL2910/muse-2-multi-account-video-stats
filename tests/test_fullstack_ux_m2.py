import sys, time
sys.path.insert(0, "/tmp/m2")
from src.contracts import *
from src.proxy.proxy_pool_manager import ProxyPoolManager, ProxyUnavailable, parse_proxy_line
from src.ingestion.pipeline import IngestionPipeline, InMemoryMetricsStore, normalize_raw_metric
from src.security.rbac_audit import *

def test_proxy_parse_and_sticky_quarantine_failover():
    info, pw = parse_proxy_line("1.2.3.4:8080:user:pass", country="VN")
    assert pw == "pass" and info.port == 8080 and info.username == "user"
    pm = ProxyPoolManager(epsilon=0.0)
    a = pm.add_proxy(ProxyInfo(id="p1", host="1.1.1.1", port=80, country="VN"))
    b = pm.add_proxy(ProxyInfo(id="p2", host="2.2.2.2", port=80, country="VN"))
    got = pm.acquire("acc1"); assert pm.acquire("acc1").id == got.id  # sticky
    for _ in range(3): pm.report_result(proxy_id=got.id, ok=False, latency_ms=10)
    assert pm.get(got.id).status == ProxyStatus.QUARANTINED
    nxt = pm.acquire("acc1"); assert nxt.id != got.id and nxt.country == "VN"  # failover same geo
    pm2 = ProxyPoolManager()
    try:
        pm2.acquire("nobody"); assert False
    except ProxyUnavailable: pass  # fail-closed

class FakeAdapter:
    def __init__(self, metrics): self.metrics = metrics
    def list_videos(self, account, *, limit=50):
        return [Video(id="v1", account_id=account.id, platform=account.platform, platform_video_id="pv1")]
    def get_video_stats(self, account, video): return self.metrics

def _acc(): return PlatformAccount(id="a1", workspace_id="w1", platform=Platform.YOUTUBE, platform_user_id="u1")

def test_pipeline_dedupe_negative_and_spike():
    acc = _acc()
    good = UnifiedVideoMetric(video_id="v1", account_id="a1", platform=Platform.YOUTUBE, platform_video_id="pv1", views=100, likes=5, comments=1)
    store = InMemoryMetricsStore(); pipe = IngestionPipeline(FakeAdapter(good), store)
    r1 = pipe.run(acc); assert r1.metrics_written == 1
    r2 = pipe.run(acc); assert r2.duplicates_skipped == 1 and len(store) == 1
    bad = UnifiedVideoMetric(video_id="v1", account_id="a1", platform=Platform.YOUTUBE, platform_video_id="pv1", views=-5)
    r3 = IngestionPipeline(FakeAdapter(bad), InMemoryMetricsStore()).run(acc)
    assert r3.needs_review == 1 and r3.metrics_written == 0
    raw = normalize_raw_metric({"viewCount": "42", "likeCount": 3}, video=Video(id="v1", account_id="a1", platform=Platform.YOUTUBE, platform_video_id="pv1"), account=acc)
    assert raw.views == 42 and raw.quality == DataQuality.PARTIAL  # comments missing -> partial, not guessed

def test_rbac_totp_audit_session():
    assert has_permission(Role.VIEWER, Permission.ACCOUNT_VIEW)
    assert not has_permission(Role.VIEWER, Permission.ACCOUNT_DELETE)
    try:
        require_permission(Role.ADMIN, Permission.ACCOUNT_VIEW, workspace_id="w1", resource_workspace_id="w2"); assert False
    except PermissionDenied: pass
    h = hash_password("s3cret"); assert verify_password("s3cret", h) and not verify_password("wrong", h)
    secret = generate_totp_secret()
    from src.security.rbac_audit import _totp_at
    assert verify_totp(secret, _totp_at(secret, int(time.time()//30)))
    chain = AuditChain()
    chain.append(workspace_id="w1", actor_id="u1", action="proxy.assign", target_type="proxy", target_id="p1", detail={"password": "x"})
    assert chain.entries()[0].detail["password"] == "***" and chain.verify_chain()
    chain._entries[0].detail["tampered"] = 1; assert not chain.verify_chain()
    sm = SessionManager(); s = sm.create(user_id="u1", workspace_id="w1", role=Role.ADMIN)
    assert sm.validate(s.token); sm.revoke(s.token); assert sm.validate(s.token) is None
    lim = LoginRateLimiter(max_failures=2)
    lim.record_failure("a@x", "1.1.1.1"); assert lim.record_failure("a@x", "1.1.1.1") and lim.is_locked("a@x", "1.1.1.1")
    print("ALL TESTS PASSED")
if __name__ == "__main__":
    test_proxy_parse_and_sticky_quarantine_failover(); test_pipeline_dedupe_negative_and_spike(); test_rbac_totp_audit_session()
