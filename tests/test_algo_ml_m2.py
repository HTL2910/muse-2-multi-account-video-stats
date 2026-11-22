"""Tests for expert/algo-ml-m2 (approach_02/06/10/14/18). Stdlib + pytest."""
import json
import math
from datetime import datetime, timedelta, timezone

import pytest

from src.accounts.account_model import (
    AccountRepository, AuditLog, InvalidTransitionError, ValidationError,
    assign_proxy, check_token_lifecycle, create_account, export_accounts_csv,
    import_accounts_csv, natural_key, transition, validate_account,
)
from src.analytics.time_series import (
    IsolationForestDetector, TimeSeriesStore, compute_derived,
    detect_anomalies, detrended_zscores, top_videos,
)
from src.ai.recommendation_engine import (
    best_posting_hours, cluster_videos, forecast_views, kmeans, recommend, tfidf_vectors,
)
from src.connectors.tiktok_connector import (
    QuotaTracker, TikTokAuthError, TikTokConnector, TikTokRateLimitError,
    map_metric, map_video,
)
from src.contracts import (
    AccountStatus, DataQuality, MetricSource, Platform, PlatformAccount,
    TokenSet, UnifiedVideoMetric, Video, utcnow,
)
from src.risk.account_health import (
    AccountCounters, AccountHealthScorerImpl, LogisticRiskModel,
    build_features, gate_action, handle_anomaly_signal, health_score_from_counters,
    redact_evidence,
)

WS = "ws-1"


def _account(**kw):
    a = create_account(workspace_id=WS, platform=Platform.TIKTOK, platform_user_id="user1", **kw)
    return a


def _metric(video_id, account_id, views, *, likes=0, comments=0, shares=0, at=None):
    return UnifiedVideoMetric(video_id=video_id, account_id=account_id, platform=Platform.TIKTOK,
                              platform_video_id="pv-" + video_id, views=views, likes=likes,
                              comments=comments, shares=shares,
                              collected_at=at or utcnow())


def test_transition_valid_and_illegal():
    audit = AuditLog()
    a = _account()
    transition(a, AccountStatus.VERIFIED, actor_id="u", audit=audit)
    transition(a, AccountStatus.ACTIVE, actor_id="u", audit=audit)
    assert a.status == AccountStatus.ACTIVE
    assert audit.verify() == []
    with pytest.raises(InvalidTransitionError):
        transition(a, AccountStatus.CREATED, actor_id="u", audit=audit)
    assert any(e.action == "account.transition_rejected" for e in audit.entries)


def test_upsert_idempotent_by_natural_key():
    repo = AccountRepository()
    a1 = _account(display_name="A")
    repo.upsert(a1)
    a2 = create_account(workspace_id=WS, platform=Platform.TIKTOK, platform_user_id="user1", display_name="B")
    out, created = repo.upsert(a2)
    assert not created and out.id == a1.id and out.display_name == "B"
    assert len(repo.list()) == 1


def test_csv_import_export_roundtrip_and_bad_row():
    repo = AccountRepository()
    text = "platform,platform_user_id,display_name,owner,niche\ntiktok,u1,One,o1,food\nbad,,X,,,\n"
    res = import_accounts_csv(text, workspace_id=WS, repo=repo)
    assert res.created == 1 and len(res.errors) == 1
    out = export_accounts_csv(repo.list())
    assert "u1" in out and "access_token" not in out


def test_token_lifecycle():
    audit = AuditLog()
    a = _account()
    transition(a, AccountStatus.VERIFIED, actor_id="u", audit=audit)
    transition(a, AccountStatus.ACTIVE, actor_id="u", audit=audit)
    check_token_lifecycle(a, TokenSet(expires_at=utcnow() + timedelta(minutes=10)), audit=audit)
    assert a.status == AccountStatus.TOKEN_EXPIRING
    check_token_lifecycle(a, TokenSet(expires_at=utcnow() - timedelta(minutes=1)), audit=audit)
    assert a.status == AccountStatus.TOKEN_EXPIRED


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def get(self, url, headers, params):
        self.calls += 1
        return 200, self.pages[min(self.calls - 1, len(self.pages) - 1)]


def _tiktok_connector():
    fixture = json.load(open("data/golden/tiktok/video_list.json"))
    http = FakeHttp([fixture])
    conn = TikTokConnector(http=http, jitter_seconds=(0, 0))
    return conn, fixture


def test_tiktok_list_and_stats_mapping():
    conn, fixture = _tiktok_connector()
    acc = _account()
    acc.credential_ref = "vault:ref1"
    videos = conn.list_videos(acc, limit=10)
    assert len(videos) == 1 and videos[0].platform_video_id == "7512345678901234567"
    raw = fixture["data"]["videos"][0]
    m = map_metric(raw, video=videos[0], account=acc)
    assert m.views == 1000 and m.likes == 120 and m.saves == 20
    assert m.source == MetricSource.TIKTOK_API and m.engagement_rate == pytest.approx(0.143)


def test_tiktok_auth_fail_closed_and_quota():
    conn = TikTokConnector(http=FakeHttp([{}]), jitter_seconds=(0, 0))
    acc = _account()
    with pytest.raises(TikTokAuthError):
        conn.list_videos(acc)
    q = QuotaTracker(requests_per_day=1, requests_per_minute=1)
    assert q.allow() and not q.allow()


def test_tiktok_supplemental_is_labelled():
    conn, _ = _tiktok_connector()
    acc = _account()
    acc.credential_ref = "vault:ref1"
    videos = conn.list_videos(acc)
    supp = conn.get_video_stats_supplemental(acc, videos[0], browser_fetcher=lambda v: {"stats": {"play_count": 1000, "collect_count": 99}})
    assert supp.source == MetricSource.BROWSER_FALLBACK and supp.saves == 99


def test_timeseries_idempotent_and_derived():
    store = TimeSeriesStore()
    t0 = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)
    for i, views in enumerate([100, 150, 300]):
        store.save_snapshot(_metric("v1", "a1", views, likes=views // 10, at=t0 + timedelta(days=i)))
    assert len(store._by_video["v1"]) == 3
    store.save_snapshot(_metric("v1", "a1", 300, at=t0 + timedelta(days=2)))
    assert len(store._by_video["v1"]) == 3
    derived = compute_derived(store.get_history(video_id="v1", start=t0, end=t0 + timedelta(days=2)))
    assert derived[-1].growth_24h == pytest.approx(1.0)
    assert derived[-1].velocity_per_hour > 0


def test_anomaly_zscore_and_iforest():
    hist = [_metric("v1", "a1", v, likes=v // 10, at=datetime(2026, 11, 22, tzinfo=timezone.utc) + timedelta(hours=i))
            for i, v in enumerate([100, 102, 101, 103, 102, 5000])]
    anomalies = detect_anomalies(hist)
    assert any(a.kind == "spike" for a in anomalies)
    det = IsolationForestDetector().fit([[float(v), 0, 0, 0] for v in [100, 101, 102, 5000]])
    assert det.score([5000.0, 0, 0, 0]) > det.score([101.0, 0, 0, 0])


def test_top_videos():
    store = TimeSeriesStore()
    t0 = datetime(2026, 11, 22, tzinfo=timezone.utc)
    for vid, views in [("v1", 100), ("v2", 1000)]:
        store.save_snapshot(_metric(vid, "a1", views, at=t0))
        store.save_snapshot(_metric(vid, "a1", views + 500, at=t0 + timedelta(hours=1)))
    assert top_videos(store, ["v1", "v2"], by="views")[0][0] == "v2"


def test_health_and_gate():
    c = AccountCounters(total_syncs=10, successful_syncs=9, requests=100, error_4xx=2, rate_limit_429=1)
    assert 0.8 < health_score_from_counters(c) <= 1.0
    assert gate_action(0.5) == ("none", 1.0)
    assert gate_action(0.75) == ("reduce_frequency", 0.5)
    assert gate_action(0.95) == ("stop", 0.0)


def test_risk_model_trains_and_explains():
    model = LogisticRiskModel()
    good = build_features(AccountCounters(total_syncs=10, successful_syncs=10))
    bad = build_features(AccountCounters(total_syncs=10, successful_syncs=2, checkpoint_count_30d=4, rate_limit_429=20, requests=20))
    model.train([(good, 0)] * 5 + [(bad, 1)] * 5)
    assert model.predict_proba(bad) > model.predict_proba(good)
    assert model.top_features(bad)


def test_checkpoint_fail_closed_no_retry():
    audit = AuditLog()
    queue = []
    a = _account()
    transition(a, AccountStatus.VERIFIED, actor_id="u", audit=audit)
    transition(a, AccountStatus.ACTIVE, actor_id="u", audit=audit)
    item = handle_anomaly_signal(a, "checkpoint", evidence={"error": "checkpoint", "token": "Bearer SECRET123"},
                                 audit=audit, manual_queue=queue)
    assert a.status == AccountStatus.CHECKPOINT
    assert queue and "SECRET123" not in item.evidence["token"]
    assert redact_evidence("access_token=abc123") == "[REDACTED]"


def test_scorer_assess():
    s = AccountHealthScorerImpl()
    s.set_counters("a1", AccountCounters(total_syncs=10, successful_syncs=10))
    h = s.score("a1")
    assert h.account_id == "a1" and h.health_score > 0.8
    assert s.assess_risk("a1").risk_level.value in ("low", "medium", "high")


def _videos():
    vs = []
    for i, (title, views, likes) in enumerate([
        ("meo tam su dem khuya", 1000, 100), ("meo an com ngon", 900, 90),
        ("cho chay bo cong vien", 100, 5), ("cho tam bien mua he", 120, 6)]):
        v = Video(id=f"v{i}", account_id="a1", platform=Platform.TIKTOK, platform_video_id=f"pv{i}",
                  title=title, published_at=datetime(2026, 11, 20 + i % 2, 10 + i, tzinfo=timezone.utc))
        vs.append(v)
    return vs


def test_tfidf_kmeans_cluster():
    vs = _videos()
    metrics = {v.id: _metric(v.id, "a1", 1000 if "meo" in v.title else 100, likes=100 if "meo" in v.title else 5) for v in vs}
    clusters = cluster_videos(vs, metrics, k=2)
    assert len(clusters) == 2
    assert clusters[0].avg_engagement >= clusters[1].avg_engagement
    assert any("meo" in c.keywords for c in clusters)


def test_forecast_bounds_and_confidence():
    t0 = datetime(2026, 11, 22, tzinfo=timezone.utc)
    hist = [_metric("v1", "a1", v, at=t0 + timedelta(hours=i * 6)) for i, v in enumerate([100, 150, 220, 320])]
    fc = forecast_views(hist, horizon="24h", video_id="v1")
    assert fc.lower_bound <= fc.predicted_views <= fc.upper_bound
    assert 0 < fc.confidence <= 1 and fc.predicted_views >= 320


def test_best_hours_and_recommend_no_autopost():
    vs = _videos()
    metrics = {v.id: _metric(v.id, "a1", 1000, likes=100) for v in vs}
    hours = best_posting_hours(vs, metrics)
    assert hours and all(0 <= h < 24 for h in hours)
    recs = recommend(account_id="a1", videos=vs, metrics=metrics)
    assert recs
