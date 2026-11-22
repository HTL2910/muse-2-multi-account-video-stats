"""fix_idempotency_hourly_bucket — contracts v1.1.0.

Kiem tra viec tach idempotency snapshot (theo gio) khoi rollup ngay:
- 2 snapshot cung video, cung ngay, khac gio: key khac nhau, luu duoc
  ca 2 (mo phong store dedup theo metric.idempotency_key).
- 2 snapshot cung gio: cung key (dedup co chu y, 1 diem/gio).
- daily_key giong nhau trong cung ngay, khac nhau qua ngay.
- CollectJob: khac scheduled_at -> khac key; cung phut -> cung key;
  job co san truong deduplicated / duplicate_of de scheduler tra ve
  job goc thay vi PENDING gia.
- Tuong thich nguoc: build_idempotency_key van la khoa ngay nhu 1.0.0,
  cac ten cu trong __all__ van import duoc, validate_metric van chay.

Stdlib + pytest only. Chay tu repo root: pytest -q
tests/test_fix_idempotency_hourly_bucket.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import (  # noqa: E402
    CONTRACTS_VERSION,
    SNAPSHOT_BUCKET_HOURS,
    CollectJob,
    JobType,
    Platform,
    UnifiedVideoMetric,
    build_daily_idempotency_key,
    build_idempotency_key,
    build_job_idempotency_key,
    build_snapshot_idempotency_key,
    compute_growth_rate,
    date_bucket,
    hour_bucket,
    validate_metric,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)
T1 = datetime(2026, 11, 22, 11, 0, tzinfo=UTC)  # cung ngay, khac gio


def _metric(views: int, collected_at: datetime) -> UnifiedVideoMetric:
    return UnifiedVideoMetric(
        video_id="vid-1",
        account_id="acc-1",
        platform=Platform.TIKTOK,
        platform_video_id="pv-1",
        views=views,
        collected_at=collected_at,
    )


def _dedup_store(metrics):
    """Mo phong MetricsStore dedup theo metric.idempotency_key."""
    seen: dict[str, UnifiedVideoMetric] = {}
    for m in metrics:
        seen.setdefault(m.idempotency_key, m)
    return seen


def test_version_is_1_1_0_and_hour_bucket_is_1h():
    assert CONTRACTS_VERSION == "1.1.0"
    assert SNAPSHOT_BUCKET_HOURS == 1


def test_hour_bucket_truncates_and_converts_to_utc():
    assert hour_bucket(datetime(2026, 11, 22, 10, 37, 45, tzinfo=UTC)) == "2026-11-22T10:00"
    # 17:20 +07:00 = 10:20 UTC -> cung bucket gio UTC 10:00
    plus7 = timezone(timedelta(hours=7))
    assert hour_bucket(datetime(2026, 11, 22, 17, 20, tzinfo=plus7)) == "2026-11-22T10:00"
    assert date_bucket(T0) == "2026-11-22"


def test_snapshot_key_differs_across_hours_same_day():
    assert build_snapshot_idempotency_key("acc-1", "pv-1", T0) != build_snapshot_idempotency_key(
        "acc-1", "pv-1", T1
    )
    assert build_snapshot_idempotency_key("acc-1", "pv-1", T0) == build_snapshot_idempotency_key(
        "acc-1", "pv-1", datetime(2026, 11, 22, 10, 15, tzinfo=UTC)
    )
    # Khac account / khac video thi khac key, cung gio.
    assert build_snapshot_idempotency_key("acc-2", "pv-1", T0) != build_snapshot_idempotency_key(
        "acc-1", "pv-1", T0
    )
    assert build_snapshot_idempotency_key("acc-1", "pv-2", T0) != build_snapshot_idempotency_key(
        "acc-1", "pv-1", T0
    )
    # Dang chuoi "YYYY-MM-DDTHH:00" cho cung ket qua voi datetime.
    assert build_snapshot_idempotency_key("acc-1", "pv-1", "2026-11-22T10:00") == build_snapshot_idempotency_key(
        "acc-1", "pv-1", T0
    )


def test_daily_key_same_within_day_differs_across_days():
    assert build_daily_idempotency_key("acc-1", "pv-1", T0) == build_daily_idempotency_key(
        "acc-1", "pv-1", T1
    )
    assert build_daily_idempotency_key("acc-1", "pv-1", T0) != build_daily_idempotency_key(
        "acc-1", "pv-1", datetime(2026, 11, 23, 10, 0, tzinfo=UTC)
    )
    # build_idempotency_key cu van la khoa ngay (tuong thich nguoc).
    assert build_idempotency_key("acc-1", "pv-1", T0) == build_daily_idempotency_key(
        "acc-1", "pv-1", T0
    )
    assert build_idempotency_key("acc-1", "pv-1", T0) == build_idempotency_key(
        "acc-1", "pv-1", T1
    )


def test_two_snapshots_same_day_different_hours_both_stored():
    m0, m1 = _metric(100, T0), _metric(300, T1)
    assert m0.idempotency_key != m1.idempotency_key
    assert m0.idempotency_key == m0.snapshot_key
    assert m0.daily_key == m1.daily_key  # rollup ngay van dung 1 key
    assert m0.hour_bucket == "2026-11-22T10:00"
    assert m1.hour_bucket == "2026-11-22T11:00"
    assert m0.date_bucket == m1.date_bucket == "2026-11-22"
    stored = _dedup_store([m0, m1])
    assert len(stored) == 2, "snapshot gio 11h bi nuot nhu loi cu (khoa ngay)"
    history = sorted(stored.values(), key=lambda m: m.collected_at)
    assert [m.views for m in history] == [100, 300]
    # Velocity / growth 1h > 0 — dung thu ma truoc day luon = 0 vi chi co 1 diem/ngay.
    assert compute_growth_rate(history[0].views, history[1].views) == 2.0


def test_two_snapshots_same_hour_dedup_intentionally():
    m0 = _metric(100, T0)
    m1 = _metric(120, datetime(2026, 11, 22, 10, 15, tzinfo=UTC))
    assert m0.idempotency_key == m1.idempotency_key
    assert len(_dedup_store([m0, m1])) == 1


def test_metric_buckets_derived_from_collected_at():
    m = _metric(50, datetime(2026, 11, 22, 17, 20, tzinfo=timezone(timedelta(hours=7))))
    assert m.date_bucket == "2026-11-22"
    assert m.hour_bucket == "2026-11-22T10:00"


def test_collect_job_key_includes_scheduled_at_minute():
    base = dict(job_type=JobType.FETCH_STATS, account_id="acc-1", platform=Platform.TIKTOK, video_id="vid-1")
    j0 = CollectJob(id="j0", scheduled_at=T0, **base)
    j1 = CollectJob(id="j1", scheduled_at=T1, **base)
    j15 = CollectJob(id="j15", scheduled_at=datetime(2026, 11, 22, 10, 15, tzinfo=UTC), **base)
    j0_dup = CollectJob(id="j0-dup", scheduled_at=T0, **base)
    # Khac gio / khac 15 phut -> khac key (khong mat job nhu loi cu).
    assert j0.idempotency_key != j1.idempotency_key
    assert j0.idempotency_key != j15.idempotency_key
    # Cung phut lap lich -> cung key (dedup dung nghia).
    assert j0.idempotency_key == j0_dup.idempotency_key
    assert j0.idempotency_key == build_job_idempotency_key(
        JobType.FETCH_STATS, "acc-1", "vid-1", T0
    )
    assert len(j0.idempotency_key) == 32
    # Khac loai job / khac video thi khac key.
    assert CollectJob(id="j2", scheduled_at=T0, job_type=JobType.SYNC_VIDEOS, account_id="acc-1", platform=Platform.TIKTOK).idempotency_key != j0.idempotency_key


def test_collect_job_explicit_key_preserved_and_dedup_flags():
    job = CollectJob(
        id="j9", job_type=JobType.FETCH_STATS, account_id="acc-1",
        platform=Platform.TIKTOK, video_id="vid-1", scheduled_at=T0,
        idempotency_key="explicit-key-123",
    )
    assert job.idempotency_key == "explicit-key-123"
    assert job.deduplicated is False
    assert job.duplicate_of is None
    # Scheduler khi dedup phai set 2 truong nay tren job goc tra ve.
    dup = CollectJob(
        id="j9-dup", job_type=JobType.FETCH_STATS, account_id="acc-1",
        platform=Platform.TIKTOK, video_id="vid-1", scheduled_at=T0,
        deduplicated=True, duplicate_of="j9",
    )
    assert dup.deduplicated is True
    assert dup.duplicate_of == "j9"


def test_no_regression_validate_metric():
    assert validate_metric(_metric(100, T0)) == []
    assert "negative views" in validate_metric(_metric(-1, T0))
