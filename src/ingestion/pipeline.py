"""approach_08 — Ingestion pipeline: Discover -> Fetch -> Normalize -> Store.

Contract: consumes ``PlatformAdapter``, produces ``UnifiedVideoMetric``,
stores via ``MetricsStore`` protocol from src/contracts.py.
Rules (fail-closed / no guessing):
- validate_metric() errors  -> quality=NEEDS_REVIEW, metric is NOT stored as OK
- missing optional fields   -> quality=PARTIAL (never fill 0 by guessing a value
  that the platform did not return; counters genuinely absent stay 0 but the
  snapshot is flagged PARTIAL so dashboards can exclude it from KPI sums)
- implausible spike vs previous snapshot -> NEEDS_REVIEW, kept out of store
- dedupe by UnifiedVideoMetric.idempotency_key (account, platform_video, day)
Stdlib only.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Iterable, Optional, Protocol

from src.contracts import (
    BatchDecision, DataQuality, JobResult, JobStatus, MetricSource, Platform,
    PlatformAccount, UnifiedVideoMetric, Video, VideoStatsSnapshot, VideoStatus,
    build_idempotency_key, date_bucket, utcnow, validate_metric,
)

__all__ = ["InMemoryMetricsStore", "IngestionPipeline", "PipelineReport",
           "normalize_raw_metric", "RAW_FIELD_MAP"]

# Raw platform field -> unified field. watch_time unified unit is SECONDS.
RAW_FIELD_MAP: dict[Platform, dict[str, str]] = {
    Platform.YOUTUBE: {"viewCount": "views", "likeCount": "likes",
                       "commentCount": "comments", "shareCount": "shares",
                       "favoriteCount": "saves", "watchTimeSeconds": "watch_time_seconds",
                       "estimatedMinutesWatched": "_minutes_watched"},
    Platform.TIKTOK: {"play_count": "views", "digg_count": "likes",
                      "comment_count": "comments", "share_count": "shares",
                      "collect_count": "saves", "watch_time_seconds": "watch_time_seconds"},
    Platform.FACEBOOK: {"video_views": "views", "likes": "likes",
                        "comments": "comments", "shares": "shares",
                        "saves": "saves", "total_video_watch_time_ms": "_watch_ms"},
}
_SOURCE: dict[Platform, MetricSource] = {
    Platform.YOUTUBE: MetricSource.YOUTUBE_DATA_API,
    Platform.TIKTOK: MetricSource.TIKTOK_API,
    Platform.FACEBOOK: MetricSource.FACEBOOK_GRAPH_API,
}
_REQUIRED_COUNTERS = ("views", "likes", "comments")


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_raw_metric(raw: dict[str, Any], *, video: Video,
                         account: PlatformAccount,
                         collected_at: Optional[datetime] = None,
                         source: Optional[MetricSource] = None) -> UnifiedVideoMetric:
    """Map one raw platform payload onto UnifiedVideoMetric (UTC, seconds)."""
    ts = collected_at or utcnow()
    mapping = RAW_FIELD_MAP[account.platform]
    values: dict[str, Any] = {}
    missing_required = False
    for raw_key, unified in mapping.items():
        if raw_key not in raw:
            continue
        val = _to_int(raw[raw_key])
        if unified == "_minutes_watched" and val is not None:
            values["watch_time_seconds"] = val * 60
        elif unified == "_watch_ms" and val is not None:
            values["watch_time_seconds"] = val // 1000
        elif not unified.startswith("_"):
            values[unified] = val if val is not None else 0
    for name in _REQUIRED_COUNTERS:
        if name not in values:
            missing_required = True
            values.setdefault(name, 0)
    quality = DataQuality.PARTIAL if missing_required else DataQuality.OK
    return UnifiedVideoMetric(
        video_id=video.id, account_id=account.id, platform=account.platform,
        platform_video_id=video.platform_video_id,
        views=values.get("views", 0), likes=values.get("likes", 0),
        comments=values.get("comments", 0), shares=values.get("shares", 0),
        saves=values.get("saves", 0),
        watch_time_seconds=values.get("watch_time_seconds", 0),
        followers_delta=_to_int(raw.get("followers_delta")) or 0,
        impressions=_to_int(raw.get("impressions", raw.get("impression_count"))),
        ctr=(float(raw["ctr"]) if isinstance(raw.get("ctr"), (int, float)) else None),
        collected_at=ts, source=source or _SOURCE[account.platform],
        quality=quality, date_bucket=date_bucket(ts))


class InMemoryMetricsStore:
    """MetricsStore protocol impl. (in-memory; production: PG + Timescale)."""

    def __init__(self) -> None:
        self._by_key: dict[str, VideoStatsSnapshot] = {}
        self._by_video: dict[str, list[UnifiedVideoMetric]] = {}

    def save_snapshot(self, metric: UnifiedVideoMetric) -> VideoStatsSnapshot:
        key = metric.idempotency_key
        if key in self._by_key:  # idempotent: same day snapshot already stored
            return self._by_key[key]
        snap = VideoStatsSnapshot(id=uuid.uuid4().hex, metric=metric)
        self._by_key[key] = snap
        self._by_video.setdefault(metric.video_id, []).append(metric)
        return snap

    def get_history(self, *, video_id: str, start: datetime, end: datetime):
        return [m for m in self._by_video.get(video_id, [])
                if start <= m.collected_at <= end]

    def get_latest(self, *, video_id: str) -> Optional[UnifiedVideoMetric]:
        rows = self._by_video.get(video_id, [])
        return max(rows, key=lambda m: m.collected_at) if rows else None

    def __len__(self) -> int:
        return len(self._by_key)


@dataclass
class PipelineReport:
    account_id: str
    videos_found: int = 0
    metrics_written: int = 0
    duplicates_skipped: int = 0
    needs_review: int = 0
    partial: int = 0
    removed_video_ids: list[str] = field(default_factory=list)
    decision: BatchDecision = BatchDecision.ACCEPT

    def to_job_result(self, job_id: str) -> JobResult:
        status = JobStatus.SUCCEEDED if self.decision in (
            BatchDecision.ACCEPT, BatchDecision.PARTIAL) else JobStatus.FAILED
        return JobResult(job_id=job_id, status=status, account_id=self.account_id,
                         videos_found=self.videos_found,
                         metrics_written=self.metrics_written,
                         decision=self.decision,
                         error_code="" if status == JobStatus.SUCCEEDED else "NEEDS_REVIEW")


class _AdapterProto(Protocol):
    def list_videos(self, account: PlatformAccount, *, limit: int = 50) -> list[Video]: ...
    def get_video_stats(self, account: PlatformAccount, video: Video) -> UnifiedVideoMetric: ...


class IngestionPipeline:
    """4-step pipeline. ``spike_ratio``: views jump > ratio AND > spike_floor
    vs previous snapshot is treated as implausible (API glitch) -> review."""

    def __init__(self, adapter: _AdapterProto, store: InMemoryMetricsStore, *,
                 spike_ratio: float = 10.0, spike_floor: int = 100_000,
                 known_video_ids: Optional[Iterable[str]] = None):
        self._adapter, self._store = adapter, store
        self._spike_ratio, self._spike_floor = spike_ratio, spike_floor
        self._known: set[str] = set(known_video_ids or [])

    def _classify(self, metric: UnifiedVideoMetric) -> DataQuality:
        if validate_metric(metric):
            return DataQuality.NEEDS_REVIEW
        prev = self._store.get_latest(video_id=metric.video_id)
        if prev is not None and prev.views > 0:
            jump = metric.views - prev.views
            if jump > self._spike_floor and metric.views > prev.views * self._spike_ratio:
                return DataQuality.NEEDS_REVIEW
            if metric.views < prev.views:  # counters are monotonic; a drop = bad data
                return DataQuality.NEEDS_REVIEW
        return metric.quality

    def run(self, account: PlatformAccount, *, job_id: str = "",
            limit: int = 50) -> PipelineReport:
        report = PipelineReport(account_id=account.id)
        # 1) Discover
        videos = self._adapter.list_videos(account, limit=limit)
        report.videos_found = len(videos)
        discovered = {v.platform_video_id for v in videos}
        report.removed_video_ids = sorted(self._known - discovered)
        self._known = discovered
        seen_keys: set[str] = set()
        for video in videos:
            if video.status in (VideoStatus.DELETED, VideoStatus.PRIVATE):
                continue
            # 2) Fetch (adapter already went through the account's sticky proxy)
            metric = self._adapter.get_video_stats(account, video)
            # 3) Normalize invariants: UTC bucket + canonical idempotency key
            if metric.date_bucket != date_bucket(metric.collected_at):
                metric = replace(metric, date_bucket=date_bucket(metric.collected_at))
            key = build_idempotency_key(metric.account_id, metric.platform_video_id,
                                        metric.date_bucket)
            if key in seen_keys:
                report.duplicates_skipped += 1
                continue
            seen_keys.add(key)
            quality = self._classify(metric)
            if quality == DataQuality.NEEDS_REVIEW:
                report.needs_review += 1
                continue  # never overwrite good data with suspect data
            if quality == DataQuality.PARTIAL:
                report.partial += 1
            # 4) Store (append-only snapshot, idempotent inside the store)
            before = len(self._store)
            self._store.save_snapshot(metric if metric.quality == quality
                                      else replace(metric, quality=quality))
            if len(self._store) > before:
                report.metrics_written += 1
            else:
                report.duplicates_skipped += 1
        if report.needs_review and report.metrics_written == 0:
            report.decision = BatchDecision.NEEDS_REVIEW
        elif report.needs_review or report.partial:
            report.decision = BatchDecision.PARTIAL
        return report
