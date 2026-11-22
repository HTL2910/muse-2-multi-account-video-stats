"""Tests for fix_youtube_quota_cache_channel (backend-sec).

Quota is a per-PROJECT budget shared by all connectors (atomic store,
UTC day roll, fail-closed when the store is unreachable); cache returns
deepcopies, never caches statistics, keys include account/project;
channel_id resolves via explicit mapping/field, not dead payload code.
Run from repo root: python -m pytest tests/test_fix_youtube_quota_cache_channel.py -q
"""
import dataclasses
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.connectors.youtube_connector import (
    InMemoryQuotaStore, QuotaExceededError, QuotaTracker, RedisQuotaStore,
    YouTubeConnector,
)
from src.contracts import AccountStatus, Platform, PlatformAccount, Video, VideoStatus

PID = "proj-test-1"


def _account(account_id="acc-1", platform_user_id="UC_plat"):
    kwargs = dict(id=account_id, platform=Platform.YOUTUBE,
                  platform_user_id=platform_user_id,
                  status=AccountStatus.ACTIVE)
    fields = {f.name for f in dataclasses.fields(PlatformAccount)}
    if "workspace_id" in fields:
        kwargs["workspace_id"] = "ws-1"
    if "channel_id" in fields:
        kwargs["channel_id"] = ""
    return PlatformAccount(**kwargs)


def _tracker(limit=10_000, project=PID, store=None):
    return QuotaTracker(daily_limit=limit, project_id=project,
                        store=store or InMemoryQuotaStore())


# ---------------------------------------------------------------- shared project quota
def test_two_connectors_share_one_project_budget():
    store = InMemoryQuotaStore()
    calls = {"n": 0}

    def transport(method, params):
        calls["n"] += 1
        return {"items": []}

    # each connector builds its OWN tracker object, same project + store
    c1 = YouTubeConnector(transport, quota=_tracker(limit=150, store=store))
    c2 = YouTubeConnector(transport, quota=_tracker(limit=150, store=store))
    acc = _account()
    video = Video(id="v1", account_id=acc.id, platform=Platform.YOUTUBE,
                  platform_video_id="vid1", status=VideoStatus.ACTIVE)

    c1.get_video_stats(acc, video)   # videos.list = 1
    c2.get_video_stats(acc, video)   # videos.list = 1
    assert c1.quota.used == 2 and c2.quota.used == 2  # SAME shared counter

    # search.list costs 100: 2 + 100 = 102 ok, another 100 would pass 150
    c1._call("search.list", {"q": "x"}, cache=False)
    with pytest.raises(QuotaExceededError):
        c2._call("search.list", {"q": "y"}, cache=False)
    # the second connector cannot spend the first one's budget twice:
    # total spent across both is still one shared 102, not 2x budgets
    assert c2.quota.used == 102


def test_default_trackers_share_via_process_store():
    t1 = QuotaTracker(daily_limit=150, project_id="proj-default-shared")
    t2 = QuotaTracker(daily_limit=150, project_id="proj-default-shared")
    t1.charge("search.list")
    assert t2.used == 100 and t2.remaining == 50
    other = QuotaTracker(daily_limit=150, project_id="proj-other")
    assert other.used == 0  # different project = independent budget


def test_quota_rolls_at_utc_day_boundary():
    now = [datetime(2026, 1, 1, 23, 59, tzinfo=timezone.utc)]
    t = QuotaTracker(daily_limit=100, project_id="proj-roll",
                     store=InMemoryQuotaStore(), now_fn=lambda: now[0])
    t.charge("search.list")
    assert t.used == 100
    with pytest.raises(QuotaExceededError):
        t.charge("videos.list")
    now[0] = datetime(2026, 1, 2, 0, 1, tzinfo=timezone.utc)  # next UTC day
    assert t.used == 0 and t.remaining == 100
    t.charge("videos.list")
    assert t.used == 1


def test_quota_store_unreachable_fails_closed():
    class BrokenStore:
        def add(self, *a):
            raise ConnectionError("redis down")

        def used(self, *a):
            raise ConnectionError("redis down")

    t = QuotaTracker(daily_limit=100, project_id="proj-broken", store=BrokenStore())
    with pytest.raises(QuotaExceededError):
        t.charge("videos.list")


class _FakeRedis:
    """Emulates the Lua add script's semantics in Python."""

    def __init__(self):
        self.data = {}

    def eval(self, script, numkeys, key, cost, limit, expire_s):
        used = self.data.get(key, 0) + cost
        if used > limit:
            return -1
        self.data[key] = used
        return used

    def get(self, key):
        return self.data.get(key)


def test_redis_quota_store_atomic_limit():
    store = RedisQuotaStore(_FakeRedis())
    t1 = QuotaTracker(daily_limit=101, project_id="proj-redis", store=store)
    t2 = QuotaTracker(daily_limit=101, project_id="proj-redis", store=store)
    t1.charge("search.list")          # 100
    t2.charge("videos.list")          # 101 — shared across "workers"
    with pytest.raises(QuotaExceededError):
        t1.charge("videos.list")      # would be 102 > 101, not added
    assert t2.used == 101


# ---------------------------------------------------------------- cache behaviour
def _stats_transport(counter):
    def transport(method, params):
        counter["n"] += 1
        return {"items": [{"id": "vid1", "statistics": {
            "viewCount": str(1000 + counter["n"]), "likeCount": "5",
            "commentCount": "2"}}]}
    return transport


def test_statistics_never_cached_snapshot_is_fresh():
    counter = {"n": 0}
    conn = YouTubeConnector(_stats_transport(counter),
                            quota=_tracker(project="proj-cache-1"))
    acc = _account()
    video = Video(id="v1", account_id=acc.id, platform=Platform.YOUTUBE,
                  platform_video_id="vid1", status=VideoStatus.ACTIVE)
    m1 = conn.get_video_stats(acc, video)
    m2 = conn.get_video_stats(acc, video)
    assert counter["n"] == 2, "statistics must hit the API every time"
    assert m2.views > m1.views, "second snapshot must not carry stale numbers"


def test_cache_returns_deepcopy_mutation_does_not_poison():
    counter = {"n": 0}

    def transport(method, params):
        counter["n"] += 1
        return {"items": [{"id": "UC1", "snippet": {"title": "Chan"}}]}

    conn = YouTubeConnector(transport, quota=_tracker(project="proj-cache-2"))
    r1 = conn._call("channels.list", {"part": "snippet", "id": "UC1"})
    r1["items"][0]["snippet"]["title"] = "HACKED"
    r1["items"].append({"id": "evil"})
    r2 = conn._call("channels.list", {"part": "snippet", "id": "UC1"})
    assert counter["n"] == 1, "metadata call should be served from cache"
    assert r2["items"] == [{"id": "UC1", "snippet": {"title": "Chan"}}]
    r2["items"][0]["snippet"]["title"] = "HACKED-2"
    r3 = conn._call("channels.list", {"part": "snippet", "id": "UC1"})
    assert r3["items"][0]["snippet"]["title"] == "Chan"


def test_cache_key_includes_account_no_cross_account_leak():
    counter = {"n": 0}

    def transport(method, params):
        counter["n"] += 1
        return {"items": [{"id": f"chan-call-{counter['n']}"}]}

    conn = YouTubeConnector(transport, quota=_tracker(project="proj-cache-3"))
    a = conn.list_channels(_account("acc-A"))
    b = conn.list_channels(_account("acc-B"))
    assert counter["n"] == 2, "same params, different account must not share cache"
    assert a[0]["id"] != b[0]["id"]
    a2 = conn.list_channels(_account("acc-A"))
    assert counter["n"] == 2 and a2[0]["id"] == a[0]["id"]  # A still cached


def test_explicit_cache_true_cannot_force_statistics_caching():
    counter = {"n": 0}
    conn = YouTubeConnector(_stats_transport(counter),
                            quota=_tracker(project="proj-cache-4"))
    conn._call("videos.list", {"part": "statistics", "id": "vid1"}, cache=True)
    conn._call("videos.list", {"part": "statistics", "id": "vid1"}, cache=True)
    assert counter["n"] == 2


# ---------------------------------------------------------------- channel_id resolution
def test_channel_id_falls_back_to_platform_user_id():
    conn = YouTubeConnector(lambda m, p: {"items": []},
                            quota=_tracker(project="proj-chan-1"))
    assert conn._channel_id(_account()) == "UC_plat"


def test_channel_id_mapping_and_explicit_field_win():
    conn = YouTubeConnector(lambda m, p: {"items": []},
                            quota=_tracker(project="proj-chan-2"))
    acc = _account()
    conn.register_channel(acc.id, "UC_mapped")
    assert conn._channel_id(acc) == "UC_mapped"
    # explicit field on the account object beats platform_user_id,
    # mapping beats the field
    ns = SimpleNamespace(id="acc-9", platform_user_id="UC_plat2",
                         channel_id="UC_explicit")
    assert conn._channel_id(ns) == "UC_explicit"
    conn.register_channel("acc-9", "UC_mapped9")
    assert conn._channel_id(ns) == "UC_mapped9"


def test_no_payload_dead_code_left():
    import inspect
    import src.connectors.youtube_connector as mod
    assert "payload" not in inspect.getsource(mod.YouTubeConnector._channel_id)


def test_list_videos_end_to_end_still_works():
    def transport(method, params):
        if method == "channels.list":
            return {"items": [{"contentDetails": {
                "relatedPlaylists": {"uploads": "UU1"}}}]}
        if method == "playlistItems.list":
            return {"items": [{"contentDetails": {"videoId": "vid1"}}]}
        if method == "videos.list":
            return {"items": [{"id": "vid1",
                               "snippet": {"title": "T", "description": "",
                                           "publishedAt": "2026-01-01T00:00:00Z",
                                           "thumbnails": {}},
                               "contentDetails": {"duration": "PT1M30S"},
                               "status": {"privacyStatus": "public"}}]}
        return {"items": []}

    conn = YouTubeConnector(transport, quota=_tracker(project="proj-e2e"))
    conn.register_channel("acc-1", "UC_mapped")
    videos = conn.list_videos(_account(), limit=10)
    assert len(videos) == 1
    assert videos[0].platform_video_id == "vid1"
    assert videos[0].duration_seconds == 90
