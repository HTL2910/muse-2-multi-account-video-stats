"""fix_ingestion_normalize_removed — khong bia 0, watch-time giay thang,
phat hien video xoa qua store.

Loi cu (da tai hien tren VM truoc khi fix):
- normalize_raw_metric gan values=0 khi _to_int tra None ma van quality
  OK nen 'abc' thanh views=0 lot vao KPI; ctr dang chuoi bi bo qua.
- YouTube duyet watchTimeSeconds truoc roi estimatedMinutesWatched sau
  nen giay bi ghi de bang phut*60 (120s -> 600s).
- _known chi song trong 1 instance nen pipeline tao moi moi job thi
  removed_video_ids luon [].

Stdlib + pytest only. Chay tu repo root:
pytest -q tests/test_fix_ingestion_normalize_removed.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import (  # noqa: E402
    AccountStatus,
    DataQuality,
    Platform,
    PlatformAccount,
    UnifiedVideoMetric,
    Video,
    VideoStatus,
)
from src.ingestion.pipeline import (  # noqa: E402
    IngestionPipeline,
    InMemoryMetricsStore,
    normalize_raw_metric,
)

T0 = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)


def _acc(platform: Platform = Platform.YOUTUBE) -> PlatformAccount:
    return PlatformAccount(
        id="acc-1", workspace_id="ws-1", platform=platform,
        platform_user_id="u1", status=AccountStatus.ACTIVE,
    )


def _vid(pid: str, platform: Platform = Platform.YOUTUBE) -> Video:
    return Video(id=f"v-{pid}", account_id="acc-1", platform=platform,
                 platform_video_id=pid, status=VideoStatus.ACTIVE)


def _yt(**kw) -> dict:
    base = {"viewCount": 100, "likeCount": 10, "commentCount": 2,
            "shareCount": 1, "favoriteCount": 0}
    base.update(kw)
    return base


# --- normalize: khong bia 0 ---------------------------------------------------

def test_unparseable_views_is_partial_not_ok_zero():
    m = normalize_raw_metric(_yt(viewCount="abc"), video=_vid("x1"), account=_acc(),
                             collected_at=T0)
    # Loi cu: views=0, quality OK -> 'abc' lot vao KPI.
    assert m.views == 0  # schema van giu 0, nhung...
    assert m.quality == DataQuality.PARTIAL  # ... phai bi danh dau de loai khoi KPI
    assert m.likes == 10  # cac truong tot van giu nguyen


def test_unparseable_optional_counter_is_partial():
    m = normalize_raw_metric(_yt(shareCount="xyz"), video=_vid("x1"), account=_acc(),
                             collected_at=T0)
    assert m.shares == 0
    assert m.quality == DataQuality.PARTIAL


def test_clean_values_stay_ok():
    m = normalize_raw_metric(_yt(), video=_vid("x1"), account=_acc(), collected_at=T0)
    assert (m.views, m.likes, m.comments, m.shares) == (100, 10, 2, 1)
    assert m.quality == DataQuality.OK


def test_missing_required_counter_is_partial():
    m = normalize_raw_metric({"likeCount": 10, "commentCount": 2},
                             video=_vid("x1"), account=_acc(), collected_at=T0)
    assert m.views == 0
    assert m.quality == DataQuality.PARTIAL


def test_numeric_string_with_comma_parses():
    m = normalize_raw_metric(_yt(viewCount="1,234"), video=_vid("x1"), account=_acc(),
                             collected_at=T0)
    assert m.views == 1234
    assert m.quality == DataQuality.OK


# --- ctr dang chuoi --------------------------------------------------------------

def test_ctr_string_parses():
    m = normalize_raw_metric(_yt(ctr="0.05"), video=_vid("x1"), account=_acc(),
                             collected_at=T0)
    assert m.ctr == pytest.approx(0.05)
    assert m.quality == DataQuality.OK


def test_ctr_percent_string_parses():
    m = normalize_raw_metric(_yt(ctr="5%"), video=_vid("x1"), account=_acc(),
                             collected_at=T0)
    assert m.ctr == pytest.approx(0.05)


def test_ctr_numeric_still_parses():
    m = normalize_raw_metric(_yt(ctr=0.12), video=_vid("x1"), account=_acc(),
                             collected_at=T0)
    assert m.ctr == pytest.approx(0.12)


def test_ctr_unparseable_is_partial_not_silently_none():
    m = normalize_raw_metric(_yt(ctr="abc"), video=_vid("x1"), account=_acc(),
                             collected_at=T0)
    assert m.ctr is None
    assert m.quality == DataQuality.PARTIAL


# --- watch-time: giay thang -------------------------------------------------------

def test_watch_time_seconds_wins_over_minutes():
    m = normalize_raw_metric(
        _yt(watchTimeSeconds=120, estimatedMinutesWatched=10),
        video=_vid("x1"), account=_acc(), collected_at=T0)
    # Loi cu: 600 (10 phut ghi de 120 giay).
    assert m.watch_time_seconds == 120
    assert m.quality == DataQuality.OK


def test_watch_time_minutes_used_only_when_no_seconds():
    m = normalize_raw_metric(
        _yt(estimatedMinutesWatched=10),
        video=_vid("x1"), account=_acc(), collected_at=T0)
    assert m.watch_time_seconds == 600
    assert m.quality == DataQuality.OK


def test_watch_time_seconds_zero_is_real_value_not_fallback():
    m = normalize_raw_metric(
        _yt(watchTimeSeconds=0, estimatedMinutesWatched=10),
        video=_vid("x1"), account=_acc(), collected_at=T0)
    assert m.watch_time_seconds == 0


def test_facebook_watch_ms_converts_to_seconds():
    acc = _acc(Platform.FACEBOOK)
    m = normalize_raw_metric(
        {"video_views": 50, "likes": 5, "comments": 1, "shares": 0,
         "saves": 0, "total_video_watch_time_ms": 90000},
        video=_vid("fb1", Platform.FACEBOOK), account=acc, collected_at=T0)
    assert m.watch_time_seconds == 90
    assert m.quality == DataQuality.OK


# --- removed detection qua store, 2 instance khac nhau -----------------------------

class _FakeAdapter:
    def __init__(self, videos):
        self._videos = list(videos)

    def list_videos(self, account, limit=50):
        return self._videos[:limit]

    def get_video_stats(self, account, video):
        return UnifiedVideoMetric(
            video_id=video.id, account_id=account.id, platform=account.platform,
            platform_video_id=video.platform_video_id, views=100, likes=10,
            comments=2, collected_at=T0)


def test_removed_detected_across_two_pipeline_instances_same_store():
    store = InMemoryMetricsStore()
    acc = _acc()
    run1 = IngestionPipeline(_FakeAdapter([_vid("a"), _vid("b")]), store).run(acc, job_id="j1")
    assert run1.metrics_written == 2
    assert run1.removed_video_ids == []
    # Instance moi hoan toan, cung store; video b bien mat khoi list.
    run2 = IngestionPipeline(_FakeAdapter([_vid("a")]), store).run(acc, job_id="j2")
    # Loi cu: removed_video_ids == [] vi _known moi rong.
    assert run2.removed_video_ids == ["b"]
    # Run tiep theo van thay b da xoa (store append-only giu lich su).
    run3 = IngestionPipeline(_FakeAdapter([_vid("a")]), store).run(acc, job_id="j3")
    assert run3.removed_video_ids == ["b"]


def test_store_known_platform_video_ids_is_scoped_by_account():
    store = InMemoryMetricsStore()
    acc1, acc2 = _acc(), PlatformAccount(
        id="acc-2", workspace_id="ws-1", platform=Platform.YOUTUBE,
        platform_user_id="u2", status=AccountStatus.ACTIVE)
    IngestionPipeline(_FakeAdapter([_vid("a")]), store).run(acc1, job_id="j1")
    assert store.known_platform_video_ids("acc-1") == {"a"}
    assert store.known_platform_video_ids("acc-2") == set()
    # Video cua acc-1 khong bi tinh la removed cua acc-2.
    run = IngestionPipeline(_FakeAdapter([]), store).run(acc2, job_id="j2")
    assert run.removed_video_ids == []


def test_removed_empty_when_nothing_was_known():
    store = InMemoryMetricsStore()
    run = IngestionPipeline(_FakeAdapter([_vid("a")]), store).run(_acc(), job_id="j1")
    assert run.removed_video_ids == []


def test_partial_metric_from_normalize_counts_partial_in_report():
    class _BadAdapter(_FakeAdapter):
        def get_video_stats(self, account, video):
            m = normalize_raw_metric(_yt(viewCount="abc"), video=video,
                                     account=account, collected_at=T0)
            return m

    store = InMemoryMetricsStore()
    report = IngestionPipeline(_BadAdapter([_vid("a")]), store).run(_acc(), job_id="j1")
    assert report.partial == 1
    assert report.metrics_written == 1  # PARTIAL van duoc luu, nhung da danh dau
