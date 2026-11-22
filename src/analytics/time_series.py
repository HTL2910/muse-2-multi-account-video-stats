"""approach_10 — Time-series store + growth analysis for video metrics.

Stdlib only. In-memory store mirrors the TimescaleDB/ClickHouse design
in TIMESCALE_DDL: append-only snapshots, idempotent on
UnifiedVideoMetric.idempotency_key, partitioned by platform+month.
Derived metrics use sliding windows; anomaly detection uses a
detrended z-score AND a small pure-python Isolation Forest (random
partition trees, deterministic seed) — no sklearn dependency, so the
module runs in CI without extra packages.

Fix ``fix_timeseries_rollup_ddl``:
- ``rollup()`` cache 300s theo (video_id, bucket_hours) nhung
  ``save_snapshot()`` khong invalidate nen dashboard thay so cu toi
  5 phut. Tu ban nay ghi snapshot moi se invalidate ngay moi rollup
  da cache cua video do (moi bucket_hours).
- DDL cu khai ``UNIQUE-on-idempotency_key-alone`` tren hypertable — bat hop
  le tren TimescaleDB (unique index phai chua cot phan vung `time`),
  va ngay ca ``UNIQUE-on-(time, idempotency_key)`` cung khong enforce
  dedup theo gio (2 snapshot cung gio, khac `time` chinh xac, van
  lot). Dedup gio song tren bang thuong ``idempotency_keys`` voi
  PRIMARY KEY la snapshot_key theo gio.
"""
from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

try:
    from src.contracts import (
        AlertType, DataQuality, UnifiedVideoMetric, VideoStatsSnapshot,
        compute_engagement_rate, compute_growth_rate, date_bucket,
        utcnow, validate_metric,
    )
except ImportError:
    from contracts import (  # type: ignore
        AlertType, DataQuality, UnifiedVideoMetric, VideoStatsSnapshot,
        compute_engagement_rate, compute_growth_rate, date_bucket,
        utcnow, validate_metric,
    )

__all__ = [
    "TIMESCALE_DDL", "DerivedMetric", "RollupBucket", "Anomaly",
    "TimeSeriesStore", "IsolationForestDetector", "detrended_zscores",
    "compute_derived", "top_videos",
]

TIMESCALE_DDL = """
-- PostgreSQL: relational tables (accounts/videos) live in approach_02.
-- TimescaleDB: hypertable for metrics, partitioned by time + platform.
--
-- Fix fix_timeseries_rollup_ddl: the old DDL declared
--   UNIQUE-on-idempotency_key-alone
-- on the hypertable. That is ILLEGAL on a TimescaleDB hypertable: a
-- unique index must contain the partitioning column `time`
-- (ERROR: "unique index ... must contain the partition column").
-- Worse, even UNIQUE-on-(time, idempotency_key) would NOT enforce the
-- hourly dedup from fix_idempotency_hourly_bucket: two snapshots in
-- the same hour have the same snapshot_key but different exact `time`
-- values, so both would be allowed. So dedup does NOT live on the
-- hypertable at all. It lives on the plain (non-hypertable) table
-- `idempotency_keys` below, whose PRIMARY KEY is the hourly
-- snapshot_key: insert the key there first (ON CONFLICT DO NOTHING);
-- only insert the metric row when the key was new. `video_metrics`
-- keeps a `snapshot_key` column (same value as `idempotency_key`
-- since contracts 1.1.0) for lookups, with a plain index.
CREATE TABLE IF NOT EXISTS video_metrics (
  time TIMESTAMPTZ NOT NULL, video_id UUID NOT NULL, account_id UUID NOT NULL,
  platform TEXT NOT NULL, platform_video_id TEXT NOT NULL,
  views BIGINT NOT NULL DEFAULT 0, likes BIGINT NOT NULL DEFAULT 0,
  comments BIGINT NOT NULL DEFAULT 0, shares BIGINT NOT NULL DEFAULT 0,
  saves BIGINT NOT NULL DEFAULT 0, watch_time_seconds BIGINT NOT NULL DEFAULT 0,
  followers_delta INT NOT NULL DEFAULT 0, source TEXT NOT NULL,
  quality TEXT NOT NULL DEFAULT 'ok', idempotency_key TEXT NOT NULL,
  snapshot_key TEXT NOT NULL
);
SELECT create_hypertable('video_metrics', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_metrics_video_time ON video_metrics (video_id, time DESC);
CREATE INDEX IF NOT EXISTS idx_metrics_snapshot_key ON video_metrics (snapshot_key);
-- Dedup table (plain PostgreSQL table, NOT a hypertable): the ONLY
-- place that enforces hourly idempotency. snapshot_key is the hourly
-- key from contracts 1.1.0 (account, platform_video, hour bucket).
-- Write path: INSERT INTO idempotency_keys (...) VALUES (...)
--   ON CONFLICT (snapshot_key) DO NOTHING;
--   -- if no row was inserted, the snapshot is a duplicate: skip the
--   -- video_metrics insert entirely.
CREATE TABLE IF NOT EXISTS idempotency_keys (
  snapshot_key TEXT PRIMARY KEY,
  video_id UUID NOT NULL, account_id UUID NOT NULL,
  platform_video_id TEXT NOT NULL,
  time TIMESTAMPTZ NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_idempotency_keys_video_time
  ON idempotency_keys (video_id, time DESC);
-- Rollups (continuous aggregates in prod): 1h / 1d, last value per bucket.
-- Retention: compress chunks > 7d, drop/archive to object storage > 365d.
-- SELECT add_compression_policy('video_metrics', INTERVAL '7 days');
-- SELECT add_retention_policy('video_metrics', INTERVAL '365 days');
"""


@dataclass
class DerivedMetric:
    video_id: str
    collected_at: datetime
    views: int
    growth_1h: float = 0.0
    growth_24h: float = 0.0
    velocity_per_hour: float = 0.0
    engagement_rate: float = 0.0
    delta_views_24h: int = 0


@dataclass
class RollupBucket:
    bucket_start: datetime
    video_id: str
    views_last: int
    views_delta: int
    likes_last: int
    comments_last: int
    shares_last: int
    engagement_rate: float


@dataclass
class Anomaly:
    video_id: str
    collected_at: datetime
    kind: str  # spike | drop | data_error
    score: float
    detail: str = ""
    alert_type: AlertType = AlertType.VIDEO_SPIKE


class _TTLCache:
    def __init__(self, ttl_seconds: float = 300.0):
        self.ttl = ttl_seconds
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str):
        import time
        item = self._store.get(key)
        if not item:
            return None
        if time.monotonic() - item[0] > self.ttl:
            del self._store[key]
            return None
        return item[1]

    def set(self, key: str, value: Any):
        import time
        self._store[key] = (time.monotonic(), value)

    def delete(self, key: str) -> None:
        self._store.pop(key, None)

    def invalidate_prefix(self, prefix: str) -> int:
        """Xoa tat ca key bat dau bang prefix. Tra ve so key da xoa."""
        doomed = [k for k in self._store if k.startswith(prefix)]
        for k in doomed:
            del self._store[k]
        return len(doomed)


class TimeSeriesStore:
    """Append-only, idempotent metrics store (MetricsStore protocol)."""

    def __init__(self, cache_ttl_seconds: float = 300.0):
        self._by_key: dict[str, VideoStatsSnapshot] = {}
        self._by_video: dict[str, list[UnifiedVideoMetric]] = defaultdict(list)
        self.cache = _TTLCache(cache_ttl_seconds)

    def invalidate_rollup(self, video_id: str) -> int:
        """Xoa moi rollup da cache cua 1 video (moi bucket_hours)."""
        return self.cache.invalidate_prefix(f"rollup:{video_id}:")

    def save_snapshot(self, metric: UnifiedVideoMetric) -> VideoStatsSnapshot:
        errors = validate_metric(metric)
        if errors:
            metric = UnifiedVideoMetric(**{**metric.__dict__, "quality": DataQuality.NEEDS_REVIEW})
        key = metric.idempotency_key
        if key in self._by_key:
            return self._by_key[key]
        snap = VideoStatsSnapshot(id=key, metric=metric, raw_payload_ref="")
        self._by_key[key] = snap
        self._by_video[metric.video_id].append(metric)
        self._by_video[metric.video_id].sort(key=lambda m: m.collected_at)
        # Fix fix_timeseries_rollup_cache_stale: rollup() cache 300s theo
        # (video_id, bucket_hours) nhung save moi khong invalidate nen
        # dashboard thay so cu toi 5 phut. Ghi moi -> xoa ngay rollup
        # da cache cua video nay (moi bucket_hours); rollup ke tiep
        # tinh lai tu du lieu moi. Duplicate (key da co) khong ghi moi
        # nen khong can invalidate.
        self.invalidate_rollup(metric.video_id)
        return snap

    def get_history(self, *, video_id: str, start: datetime, end: datetime) -> list[UnifiedVideoMetric]:
        return [m for m in self._by_video.get(video_id, []) if start <= m.collected_at <= end]

    def get_latest(self, *, video_id: str) -> Optional[UnifiedVideoMetric]:
        hist = self._by_video.get(video_id, [])
        return hist[-1] if hist else None

    def partition_key(self, metric: UnifiedVideoMetric) -> str:
        """platform + YYYY-MM, matching the hypertable partition scheme."""
        return f"{metric.platform.value}:{metric.collected_at.strftime('%Y-%m')}"

    def rollup(self, *, video_id: str, bucket_hours: int = 24) -> list[RollupBucket]:
        cache_key = f"rollup:{video_id}:{bucket_hours}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached
        hist = sorted(self._by_video.get(video_id, []), key=lambda m: m.collected_at)
        buckets: dict[datetime, list[UnifiedVideoMetric]] = defaultdict(list)
        for m in hist:
            epoch = m.collected_at.replace(minute=0, second=0, microsecond=0)
            start = epoch - timedelta(hours=epoch.hour % bucket_hours)
            buckets[start].append(m)
        out: list[RollupBucket] = []
        for start in sorted(buckets):
            ms = buckets[start]
            first, last = ms[0], ms[-1]
            out.append(RollupBucket(
                bucket_start=start, video_id=video_id, views_last=last.views,
                views_delta=max(0, last.views - first.views),
                likes_last=last.likes, comments_last=last.comments, shares_last=last.shares,
                engagement_rate=compute_engagement_rate(last.views, last.likes, last.comments, last.shares)))
        self.cache.set(cache_key, out)
        return out


def compute_derived(history: list[UnifiedVideoMetric]) -> list[DerivedMetric]:
    """Sliding-window derived metrics. Views are cumulative, so growth
    is computed against the snapshot ~1h / ~24h earlier (nearest)."""
    hist = sorted(history, key=lambda m: m.collected_at)
    out: list[DerivedMetric] = []
    for i, m in enumerate(hist):
        def _nearest(hours: float) -> Optional[UnifiedVideoMetric]:
            # Only earlier snapshots: including the current point would
            # always win (distance = `hours`) and give velocity 0.
            target = m.collected_at - timedelta(hours=hours)
            return min(hist[:i], key=lambda x: abs((x.collected_at - target).total_seconds()), default=None) if i else None
        p1 = _nearest(1)
        p24 = _nearest(24)
        growth_1h = compute_growth_rate(p1.views, m.views) if p1 and p1 is not m else 0.0
        growth_24h = compute_growth_rate(p24.views, m.views) if p24 and p24 is not m else 0.0
        velocity = 0.0
        if p1 and p1 is not m:
            dt = (m.collected_at - p1.collected_at).total_seconds() / 3600.0
            if dt > 0:
                velocity = max(0, m.views - p1.views) / dt
        out.append(DerivedMetric(
            video_id=m.video_id, collected_at=m.collected_at, views=m.views,
            growth_1h=growth_1h if math.isfinite(growth_1h) else 0.0,
            growth_24h=growth_24h if math.isfinite(growth_24h) else 0.0,
            velocity_per_hour=velocity,
            engagement_rate=compute_engagement_rate(m.views, m.likes, m.comments, m.shares),
            delta_views_24h=max(0, m.views - p24.views) if p24 else 0))
    return out


def detrended_zscores(values: list[float]) -> list[float]:
    """First-difference (detrend) then z-score. Empty / flat -> all 0."""
    if len(values) < 3:
        return [0.0] * len(values)
    diffs = [values[i] - values[i - 1] for i in range(1, len(values))]
    mean = statistics.fmean(diffs)
    sd = statistics.pstdev(diffs) or 0.0
    if sd == 0:
        return [0.0] * len(values)
    return [0.0] + [(d - mean) / sd for d in diffs]


class IsolationForestDetector:
    """Small deterministic Isolation Forest for multi-dimensional points."""

    def __init__(self, n_trees: int = 50, max_depth: int = 8, seed: int = 42):
        self.n_trees = n_trees
        self.max_depth = max_depth
        self._rng = random.Random(seed)
        self._trees: list[Any] = []
        self._n = 0

    @staticmethod
    def _c(n: int) -> float:
        if n <= 1:
            return 0.0
        return 2 * (math.log(n - 1) + 0.5772156649) - 2 * (n - 1) / n

    def _build(self, points: list[list[float]], depth: int) -> Any:
        if depth >= self.max_depth or len(points) <= 1:
            return ("leaf", len(points))
        dim = self._rng.randrange(len(points[0]))
        vals = [p[dim] for p in points]
        lo, hi = min(vals), max(vals)
        if lo == hi:
            return ("leaf", len(points))
        split = self._rng.uniform(lo, hi)
        left = [p for p in points if p[dim] < split]
        right = [p for p in points if p[dim] >= split]
        if not left or not right:
            return ("leaf", len(points))
        return ("node", dim, split, self._build(left, depth + 1), self._build(right, depth + 1))

    def fit(self, points: list[list[float]]) -> "IsolationForestDetector":
        if not points:
            raise ValueError("no points to fit")
        self._n = len(points)
        self._trees = [self._build(points, 0) for _ in range(self.n_trees)]
        return self

    def _depth(self, tree: Any, point: list[float], depth: int = 0) -> float:
        if tree[0] == "leaf":
            return depth + self._c(tree[1])
        _, dim, split, left, right = tree
        return self._depth(left if point[dim] < split else right, point, depth + 1)

    def score(self, point: list[float]) -> float:
        if not self._trees:
            raise ValueError("detector not fitted")
        avg = sum(self._depth(t, point) for t in self._trees) / len(self._trees)
        c = self._c(self._n) or 1.0
        return 2 ** (-avg / c)

    def anomalies(self, points: list[list[float]], *, threshold: float = 0.65) -> list[int]:
        return [i for i, p in enumerate(points) if self.score(p) >= threshold]


def detect_anomalies(history: list[UnifiedVideoMetric], *, z_threshold: float = 3.0,
                     iforest_threshold: float = 0.65) -> list[Anomaly]:
    """Combine z-score (univariate, detrended views) and Isolation Forest
    (views, likes, comments, shares). A drop in cumulative views is a
    data error, never a real 'drop' — flag it, do not overwrite history."""
    hist = sorted(history, key=lambda m: m.collected_at)
    out: list[Anomaly] = []
    if len(hist) < 3:
        return out
    views = [float(m.views) for m in hist]
    for i, z in enumerate(detrended_zscores(views)):
        if i == 0:
            continue
        # A single huge jump drowns the z-score (it inflates mean/sd), so
        # a relative-growth rule is needed alongside it: >200% in one step.
        growth = compute_growth_rate(hist[i - 1].views, hist[i].views)
        if growth > 2.0:
            out.append(Anomaly(hist[i].video_id, hist[i].collected_at, "spike", growth,
                                f"views grew {growth:.1%} in one step", AlertType.VIDEO_SPIKE))
            continue
        if hist[i].views < hist[i - 1].views:
            out.append(Anomaly(hist[i].video_id, hist[i].collected_at, "data_error", abs(z),
                                f"views fell {hist[i-1].views} -> {hist[i].views}", AlertType.VIDEO_DROP))
        elif z >= z_threshold:
            out.append(Anomaly(hist[i].video_id, hist[i].collected_at, "spike", z,
                                f"detrended z={z:.2f}", AlertType.VIDEO_SPIKE))
        elif z <= -z_threshold:
            out.append(Anomaly(hist[i].video_id, hist[i].collected_at, "drop", abs(z),
                                f"detrended z={z:.2f}", AlertType.VIDEO_DROP))
    points = [[float(m.views), float(m.likes), float(m.comments), float(m.shares)] for m in hist]
    cols = list(zip(*points))
    norm = [[(p[d] - min(cols[d])) / (max(cols[d]) - min(cols[d]) or 1.0) for d in range(4)] for p in points]
    det = IsolationForestDetector().fit(norm)
    for i in det.anomalies(norm, threshold=iforest_threshold):
        if not any(a.collected_at == hist[i].collected_at for a in out):
            out.append(Anomaly(hist[i].video_id, hist[i].collected_at, "spike",
                               det.score(norm[i]), "isolation-forest outlier", AlertType.VIDEO_SPIKE))
    return sorted(out, key=lambda a: a.collected_at)


def top_videos(store: TimeSeriesStore, video_ids: list[str], *,
               by: str = "velocity", limit: int = 10) -> list[tuple[str, float]]:
    scored: list[tuple[str, float]] = []
    for vid in video_ids:
        hist = store._by_video.get(vid, [])
        if not hist:
            continue
        derived = compute_derived(hist)
        last = derived[-1] if derived else None
        if not last:
            continue
        score = {"velocity": last.velocity_per_hour, "growth_24h": last.growth_24h,
                 "engagement": last.engagement_rate, "views": float(last.views)}.get(by, last.velocity_per_hour)
        scored.append((vid, score))
    return sorted(scored, key=lambda x: x[1], reverse=True)[:limit]
