"""fix_timeseries_rollup_ddl — rollup thay ngay sau khi ghi, DDL hop le.

Loi cu (da tai hien tren VM truoc khi fix):
- rollup() cache 300s theo (video_id, bucket_hours) nhung
  save_snapshot() khong invalidate: luu snapshot 300 views ngay hom
  sau roi rollup van chi thay bucket cu voi 100 views.
- TIMESCALE_DDL khai UNIQUE (idempotency_key) tren hypertable: bat hop
  le tren TimescaleDB (unique index phai chua cot phan vung `time`),
  va ngay ca UNIQUE (time, idempotency_key) cung khong enforce dedup
  theo gio (2 snapshot cung gio, khac `time` chinh xac, van lot).

Chu y ve kiem chung DDL: tren VM nay khong co docker/psql nen KHONG
the chay TimescaleDB that o day; test duoi day kiem chung DDL bang
phan tich tinh (khong con UNIQUE bat hop le tren hypertable, co bang
idempotency_keys voi PRIMARY KEY snapshot_key) + mo phong semantics
dedup bang sqlite3 (stdlib). Buoc chay DDL tren TimescaleDB that
trong docker-compose la buoc CI, khong duoc claim la da chay o day.

Stdlib + pytest only. Chay tu repo root:
pytest -q tests/test_fix_timeseries_rollup_ddl.py
"""
from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.analytics.time_series import TIMESCALE_DDL, TimeSeriesStore  # noqa: E402
from src.contracts import Platform, UnifiedVideoMetric  # noqa: E402

T0 = datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc)


def _metric(video_id: str, views: int, *, at: datetime) -> UnifiedVideoMetric:
    return UnifiedVideoMetric(
        video_id=video_id, account_id="acc-1", platform=Platform.TIKTOK,
        platform_video_id=f"pv-{video_id}", views=views, likes=10,
        comments=2, collected_at=at)


# --- Rollup thay ngay sau khi ghi ----------------------------------------------

def test_rollup_sees_new_snapshot_immediately():
    store = TimeSeriesStore()
    store.save_snapshot(_metric("v1", 100, at=T0))
    first = store.rollup(video_id="v1")
    assert [b.views_last for b in first] == [100]
    # Snapshot moi ngay hom sau (key theo gio khac nhau -> khong dedup).
    store.save_snapshot(_metric("v1", 300, at=T0 + timedelta(days=1)))
    second = store.rollup(video_id="v1")
    # Loi cu: second van chi co [(20/11, 100)] — stale toi 5 phut.
    assert [b.views_last for b in second] == [100, 300]
    assert second[-1].bucket_start == datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc)


def test_rollup_cache_still_works_when_nothing_was_written():
    store = TimeSeriesStore()
    store.save_snapshot(_metric("v1", 100, at=T0))
    first = store.rollup(video_id="v1")
    second = store.rollup(video_id="v1")
    assert second is first  # cache hit: khong ghi moi thi khong tinh lai
    assert [b.views_last for b in second] == [100]


def test_invalidation_covers_all_bucket_hours():
    store = TimeSeriesStore()
    store.save_snapshot(_metric("v1", 100, at=T0))
    assert [b.views_last for b in store.rollup(video_id="v1", bucket_hours=24)] == [100]
    assert [b.views_last for b in store.rollup(video_id="v1", bucket_hours=1)] == [100]
    store.save_snapshot(_metric("v1", 300, at=T0 + timedelta(days=1)))
    assert [b.views_last for b in store.rollup(video_id="v1", bucket_hours=24)] == [100, 300]
    assert [b.views_last for b in store.rollup(video_id="v1", bucket_hours=1)] == [100, 300]


def test_duplicate_snapshot_does_not_change_rollup():
    store = TimeSeriesStore()
    m = _metric("v1", 100, at=T0)
    store.save_snapshot(m)
    first = store.rollup(video_id="v1")
    store.save_snapshot(m)  # cung key theo gio -> duplicate, khong ghi moi
    assert store.rollup(video_id="v1") is first
    assert [b.views_last for b in store.rollup(video_id="v1")] == [100]


def test_invalidation_is_scoped_to_one_video():
    store = TimeSeriesStore()
    store.save_snapshot(_metric("v1", 100, at=T0))
    store.save_snapshot(_metric("v2", 50, at=T0))
    cached_v1 = store.rollup(video_id="v1")
    store.save_snapshot(_metric("v2", 80, at=T0 + timedelta(days=1)))
    # Chi v2 bi invalidate; rollup cua v1 van la object da cache.
    assert store.rollup(video_id="v1") is cached_v1
    assert [b.views_last for b in store.rollup(video_id="v2")] == [50, 80]


def test_invalidate_rollup_returns_count():
    store = TimeSeriesStore()
    store.save_snapshot(_metric("v1", 100, at=T0))
    store.rollup(video_id="v1", bucket_hours=24)
    store.rollup(video_id="v1", bucket_hours=1)
    assert store.invalidate_rollup("v1") == 2
    assert store.invalidate_rollup("v1") == 0


# --- DDL: khong con UNIQUE bat hop le tren hypertable ---------------------------

def _video_metrics_block() -> str:
    m = re.search(r"CREATE TABLE IF NOT EXISTS video_metrics \((.*?)\);",
                  TIMESCALE_DDL, re.S)
    assert m, "DDL thieu bang video_metrics"
    return m.group(1)


def test_ddl_video_metrics_has_no_unique_constraint():
    block = _video_metrics_block()
    assert "UNIQUE" not in block.upper(), (
        "UNIQUE tren hypertable la bat hop le: unique index phai chua "
        "cot phan vung `time`, va UNIQUE (time, idempotency_key) cung "
        "khong enforce dedup theo gio")
    assert "snapshot_key TEXT NOT NULL" in block


def test_ddl_has_create_hypertable_and_snapshot_index():
    assert "create_hypertable('video_metrics', 'time'" in TIMESCALE_DDL
    assert "idx_metrics_snapshot_key" in TIMESCALE_DDL
    assert "idx_metrics_video_time" in TIMESCALE_DDL


def test_ddl_idempotency_keys_table_enforces_hourly_dedup():
    m = re.search(r"CREATE TABLE IF NOT EXISTS idempotency_keys \((.*?)\);",
                  TIMESCALE_DDL, re.S)
    assert m, "DDL thieu bang idempotency_keys rieng de dedup"
    block = m.group(1)
    assert "snapshot_key TEXT PRIMARY KEY" in block
    # Bang dedup la bang thuong, KHONG phai hypertable.
    assert "create_hypertable('idempotency_keys'" not in TIMESCALE_DDL
    assert "ON CONFLICT (snapshot_key) DO NOTHING" in TIMESCALE_DDL


def test_ddl_idempotency_keys_semantics_in_sqlite():
    """Mo phong semantics cua bang idempotency_keys bang sqlite3:
    cung snapshot_key (cung gio) thi chi insert duoc 1 lan; gio khac
    thi insert duoc. Day la hanh vi ma DDL Postgres phai co."""
    con = sqlite3.connect(":memory:")
    con.execute("""
        CREATE TABLE idempotency_keys (
          snapshot_key TEXT PRIMARY KEY,
          video_id TEXT NOT NULL, account_id TEXT NOT NULL,
          platform_video_id TEXT NOT NULL,
          time TEXT NOT NULL,
          created_at TEXT NOT NULL DEFAULT 'now'
        )""")
    key_hour_10 = "acc-1:pv-v1:2026-11-20T10:00"
    key_hour_11 = "acc-1:pv-v1:2026-11-20T11:00"
    cur = con.execute(
        "INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?, ?)",
        (key_hour_10, "v1", "acc-1", "pv-v1", "2026-11-20T10:00:00Z", "now"))
    assert cur.rowcount == 1
    # Cung gio, khac `time` chinh xac: van phai bi chan (ly do khong
    # dung UNIQUE (time, snapshot_key) tren hypertable).
    cur = con.execute(
        "INSERT OR IGNORE INTO idempotency_keys VALUES (?, ?, ?, ?, ?, ?)",
        (key_hour_10, "v1", "acc-1", "pv-v1", "2026-11-20T10:37:00Z", "now"))
    assert cur.rowcount == 0  # duplicate: khong insert them
    cur = con.execute(
        "INSERT INTO idempotency_keys VALUES (?, ?, ?, ?, ?, ?)",
        (key_hour_11, "v1", "acc-1", "pv-v1", "2026-11-20T11:00:00Z", "now"))
    assert cur.rowcount == 1  # gio khac: insert duoc
    assert con.execute("SELECT COUNT(*) FROM idempotency_keys").fetchone()[0] == 2
    con.close()
