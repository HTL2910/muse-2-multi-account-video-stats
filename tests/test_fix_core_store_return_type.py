"""fix_core_store_return_type — save_snapshot tra VideoStatsSnapshot.

Contracts MetricsStore.save_snapshot() khai bao tra VideoStatsSnapshot,
ingestion/analytics tra dung kieu, nhung core tra thang UnifiedVideoMetric
nen code goi qua protocol va truy cap .metric se AttributeError.

Test tu dua repo root len sys.path de khong phu thuoc conftest/pyproject.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.contracts import (
    Platform,
    UnifiedVideoMetric,
    VideoStatsSnapshot,
    utcnow,
)
from src.core.models import InMemoryMetricsStore


def _metric(**overrides) -> UnifiedVideoMetric:
    base = dict(
        video_id="vid-1",
        account_id="acc-1",
        platform=Platform.FACEBOOK,
        platform_video_id="fb-987",
        views=100,
        likes=5,
        comments=2,
        shares=1,
        watch_time_seconds=1234,
        date_bucket="2026-11-22",
        collected_at=datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return UnifiedVideoMetric(**base)


def test_save_snapshot_returns_video_stats_snapshot():
    store = InMemoryMetricsStore()
    metric = _metric()
    result = store.save_snapshot(metric)
    assert isinstance(result, VideoStatsSnapshot), type(result)
    assert not isinstance(result, UnifiedVideoMetric)
    assert result.metric is metric
    assert result.metric.views == 100
    assert result.id  # id moi, khong rong
    assert result.raw_payload_ref == ""


def test_save_snapshot_is_idempotent_and_returns_same_snapshot():
    store = InMemoryMetricsStore()
    metric = _metric()
    first = store.save_snapshot(metric)
    second = store.save_snapshot(metric)  # cung idempotency_key
    assert second is first
    assert second.id == first.id
    start = datetime(2026, 11, 22, 0, 0, tzinfo=timezone.utc)
    end = datetime(2026, 11, 22, 23, 59, tzinfo=timezone.utc)
    assert len(store.get_history(video_id="vid-1", start=start, end=end)) == 1


def test_history_and_latest_still_return_metrics():
    store = InMemoryMetricsStore()
    store.save_snapshot(_metric(views=100))
    # Luu y: sau fix_idempotency_hourly_bucket, idempotency_key tinh theo
    # hour_bucket (tu collected_at), khong phai chi date_bucket. Metric
    # thu 2 chi khac date_bucket nhung cung collected_at 10:00 se bi dedup
    # dung luat va get_latest van la 100 — do la loi cua fixture test nay
    # (1 failed o CI truoc), khong phai loi cua save_snapshot(). De co
    # snapshot thu 2 that su, phai doi ca collected_at sang gio khac.
    store.save_snapshot(_metric(
        views=300, date_bucket="2026-11-23",
        collected_at=datetime(2026, 11, 23, 10, 0, tzinfo=timezone.utc),
    ))
    assert store.get_latest(video_id="vid-1").views == 300
    assert store.get_latest(video_id="missing") is None
    rollup = store.daily_rollup(video_id="vid-1", date_bucket="2026-11-22")
    assert rollup is not None and rollup.views == 100


def test_same_hour_bucket_is_deduped_even_if_date_differs():
    """Hoi quy cho idempotency theo gio: cung hour_bucket thi dedup,
    du date_bucket co khac (chung minh test tren khong phai sua code)."""
    store = InMemoryMetricsStore()
    first = store.save_snapshot(_metric(views=100))
    second = store.save_snapshot(_metric(views=300, date_bucket="2026-11-23"))
    assert second is first
    assert store.get_latest(video_id="vid-1").views == 100


def test_invalid_metric_still_rejected_before_snapshot():
    store = InMemoryMetricsStore()
    with pytest.raises(ValueError):
        store.save_snapshot(_metric(views=-1))


def test_protocol_style_access_does_not_raise_attribute_error():
    """Dung nhu code goi qua protocol: result.metric.<field>."""
    store = InMemoryMetricsStore()
    snapshot = store.save_snapshot(_metric())
    assert snapshot.metric.engagement_rate == pytest.approx(0.08)
    assert snapshot.metric.idempotency_key
