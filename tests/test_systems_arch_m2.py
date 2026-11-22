"""Tests for expert/systems-arch-m2 (approach_01,05,09,13,17).
Run: /tmp/m2/venv/bin/python -m pytest tests -q  (from /tmp/m2)
Uses the local stub contracts.py — real contracts.py on main has the
same fields/signatures; CI must re-run against main.
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import (AccountStatus, BatchDecision, CollectJob, DataQuality,
    GoldenCase, JobPriority, JobStatus, JobType, MetricSource, Platform,
    PlatformAccount, ProxyInfo, ProxyStatus, TokenSet, UnifiedVideoMetric,
    Video, utcnow)
from src.core.models import (CoreService, InMemoryMetricsStore, transition_account,
    AccountTransitionError, should_split_to_microservices)
from src.connectors.facebook_connector import (FacebookConnector, AdapterAuthError,
    parse_facebook_video, parse_facebook_insights)
from src.scheduler.task_scheduler import (TaskSchedulerImpl, TokenBucket,
    ConsistentHashRing, schedule_jobs_for_account)
from src.observability.monitor import (Monitor, validate_batch, fail_closed_gate,
    KillSwitch, GoldenRunner, redact)
from src.api.main import create_app, InMemoryStore
from fastapi.testclient import TestClient

def _acc(status=AccountStatus.ACTIVE):
    return PlatformAccount(id="acc1", workspace_id="ws1", platform=Platform.FACEBOOK,
                           platform_user_id="123", credential_ref="vault://acc1", status=status)

# approach_01
def test_transition_illegal_rejected():
    a = _acc(AccountStatus.CREATED)
    try:
        transition_account(a, AccountStatus.BANNED); assert False
    except AccountTransitionError: pass
    transition_account(a, AccountStatus.VERIFIED)
    assert a.status == AccountStatus.VERIFIED

def test_split_threshold():
    assert should_split_to_microservices(account_count=500, videos_per_day=1)
    assert should_split_to_microservices(account_count=10, videos_per_day=10000)
    assert not should_split_to_microservices(account_count=10, videos_per_day=100)

def test_metrics_store_idempotent():
    s = InMemoryMetricsStore()
    m = UnifiedVideoMetric(video_id="v1", account_id="acc1", platform=Platform.FACEBOOK, platform_video_id="123", views=10)
    s.save_snapshot(m); s.save_snapshot(m)
    assert s.get_latest(video_id="v1").views == 10
    assert len(s.get_history(video_id="v1", start=m.collected_at, end=m.collected_at)) == 1

def test_can_collect_fail_closed():
    core = CoreService()
    proxy = ProxyInfo(id="p1", host="1.2.3.4", port=8000, status=ProxyStatus.DEAD)
    ok, _ = core.can_collect(_acc(AccountStatus.CHECKPOINT), proxy)
    assert not ok
    ok, _ = core.can_collect(_acc(), None)
    assert not ok
    ok, _ = core.can_collect(_acc(), ProxyInfo(id="p2", host="1.2.3.4", port=8000, status=ProxyStatus.ACTIVE))
    assert ok

# approach_05
def _fake_http_factory():
    def http_get(url, headers):
        if "/123/videos" in url:
            return {"data": [{"id": "987", "title": "T", "description": "D", "created_time": "2026-11-20T10:00:00+00:00",
                              "permalink_url": "https://fb.watch/987", "length": 60,
                              "likes": {"summary": {"total_count": 5}}, "comments": {"summary": {"total_count": 2}}, "shares": {"count": 1}}], "paging": {}}
        if "/987/video_insights" in url:
            return {"data": [{"name": "post_video_views", "values": [{"value": 100}]},
                            {"name": "post_video_view_time", "values": [{"value": 1234}]},
                            {"name": "post_impressions", "values": [{"value": 500}]}]}
        if "/987" in url:
            return {"id": "987", "views": 100, "likes": {"summary": {"total_count": 5}},
                    "comments": {"summary": {"total_count": 2}}, "shares": {"count": 1}}
        if "/me" in url:
            return {"id": "123", "name": "Page"}
        raise AssertionError(url)
    return http_get

def test_facebook_list_and_stats():
    conn = FacebookConnector(token_resolver=lambda a: TokenSet(access_token="tok"), http_get=_fake_http_factory())
    acc = _acc()
    videos = conn.list_videos(acc, limit=10)
    assert len(videos) == 1 and videos[0].platform_video_id == "987"
    assert conn.check_alive(acc) is True
    m = conn.get_video_stats(acc, videos[0])
    assert m.views == 100 and m.likes == 5 and m.watch_time_seconds == 1234
    assert m.source == MetricSource.FACEBOOK_GRAPH_API and m.quality == DataQuality.OK

def test_facebook_auth_fail_closed():
    def bad(url, headers):
        return {"error": {"code": 190, "message": "token expired"}}
    conn = FacebookConnector(token_resolver=lambda a: TokenSet(access_token="tok"), http_get=bad)
    try:
        conn.list_videos(_acc()); assert False
    except AdapterAuthError: pass

def test_golden_facebook_fixture():
    raw = json.loads(Path("data/golden/facebook/video_list.json").read_text())
    acc = _acc()
    v = parse_facebook_video(raw["data"][0], account=acc)
    m = parse_facebook_insights(raw["insights"], raw["data"][0], video=v, account=acc)
    assert m.views == 100 and m.likes == 5

# approach_09
def test_scheduler_priority_and_dedup_and_dead_letter():
    s = TaskSchedulerImpl(workers=["worker-1"], rng=__import__("random").Random(0))
    s.account_status["acc1"] = AccountStatus.ACTIVE
    job = CollectJob(id="j1", job_type=JobType.FETCH_STATS, account_id="acc1", platform=Platform.FACEBOOK)
    assert s.enqueue(job).status == JobStatus.PENDING
    job2 = CollectJob(id="j2", job_type=JobType.FETCH_STATS, account_id="acc1", platform=Platform.FACEBOOK)
    assert s.enqueue(job2).status == JobStatus.PENDING
    got = s.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK)
    assert got is not None and got.status == JobStatus.RUNNING
    # retry 3x -> dead letter (moi lan worker phai acquire lai, attempts tang o acquire)
    st = None
    for _ in range(3):
        got = s.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK) or got
        st = s.finish_job(got, ok=False, error="x")
    assert st == JobStatus.DEAD_LETTER
    assert len(s.dead_letter.list()) == 1
    back = s.requeue_dead_letter(got.id)
    assert back.status == JobStatus.PENDING

def test_scheduler_fail_closed_reject():
    s = TaskSchedulerImpl(workers=["worker-1"])
    s.account_status["acc1"] = AccountStatus.BANNED
    job = CollectJob(id="j9", job_type=JobType.FETCH_STATS, account_id="acc1", platform=Platform.FACEBOOK)
    assert s.enqueue(job).status == JobStatus.REJECTED

def test_token_bucket_and_ring():
    b = TokenBucket(rate=10, capacity=2)
    assert b.try_consume() and b.try_consume() and not b.try_consume()
    ring = ConsistentHashRing(["w1", "w2"])
    assert ring.worker_for("acc1") == ring.worker_for("acc1")

def test_schedule_intervals():
    jobs = schedule_jobs_for_account(_acc(), videos=[{"video_id": "v1", "is_new": True}, {"video_id": "v2", "is_new": False}])
    assert jobs[0].job_type == JobType.SYNC_VIDEOS
    assert jobs[1].priority == JobPriority.HIGH

# approach_13
def test_api_crud_and_sync():
    app = create_app(store=InMemoryStore(), auth_checker=lambda t: {"sub": "u1", "workspace_id": "ws1"})
    c = TestClient(app)
    r = c.post("/accounts", json={"workspace_id": "ws1", "platform": "facebook", "platform_user_id": "123", "credential_ref": "vault://x"})
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    r = c.get("/accounts", params={"workspace_id": "ws1"})
    assert r.status_code == 200 and r.json()["total"] == 1
    r = c.post(f"/accounts/{aid}/sync-videos")
    assert r.status_code == 200 and r.json()["status"] == "pending"
    r = c.get("/videos")
    assert r.status_code == 200
    r = c.get("/accounts", params={"workspace_id": "ws1", "cursor": "!!!"})
    assert r.status_code == 400

# approach_17
def test_batch_gate_and_redact():
    m = UnifiedVideoMetric(video_id="v1", account_id="acc1", platform=Platform.FACEBOOK, platform_video_id="987", views=100)
    assert validate_batch([m], expected_video_count=1).decision == BatchDecision.ACCEPT
    assert validate_batch([m], expected_video_count=2).decision == BatchDecision.REJECT
    bad = UnifiedVideoMetric(video_id="v1", account_id="acc1", platform=Platform.FACEBOOK, platform_video_id="987", views=-1)
    assert validate_batch([bad]).decision == BatchDecision.REJECT
    assert "tok123" not in redact("access_token=tok123 Bearer abcdefgh")
    assert "***" in redact("access_token=tok123")

def test_fail_closed_gate_and_kill_switch():
    ok, _ = fail_closed_gate(_acc(AccountStatus.BANNED), ProxyInfo(id="p", host="h", port=1))
    assert not ok
    ks = KillSwitch(error_rate_threshold=0.9, window=10, min_samples=5)
    for _ in range(5): ks.record(False)
    assert ks.tripped

def test_monitor_prometheus_and_golden():
    mon = Monitor()
    mon.record_job(ok=True, platform=Platform.FACEBOOK, job_status=JobStatus.SUCCEEDED)
    assert "jobs_total" in mon.prometheus_text()
    def load(p): return json.loads(Path(p).read_text())
    def amap(platform, raw):
        acc = _acc(); v = parse_facebook_video(raw, account=acc)
        return parse_facebook_insights(json.loads(Path("data/golden/facebook/video_list.json").read_text())["insights"], raw, video=v, account=acc)
    runner = GoldenRunner(load_fixture=load, adapter_map=amap)
    case = GoldenCase(id="g1", platform=Platform.FACEBOOK, name="fb list", raw_fixture_path="data/golden/facebook/video_list.json", expected_video_count=1,
                      expected_metric=UnifiedVideoMetric(video_id="x", account_id="acc1", platform=Platform.FACEBOOK, platform_video_id="987", views=100, likes=5, comments=2, shares=1))
    ok, errs = runner.run(case)
    assert ok, errs
