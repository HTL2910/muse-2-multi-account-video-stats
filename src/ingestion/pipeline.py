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

Fix ``fix_ingestion_normalize_removed``:
- Gia tri khong parse duoc (vd views='abc') khong con bi gan 0 lang le
  voi quality OK nua: truong do duoc danh dau missing, metric bi
  PARTIAL (va van giu 0 o truong so de schema khong vo, nhung dashboard
  phai loai PARTIAL khoi KPI). ``ctr`` dang chuoi ("0.05", "5%") duoc
  parse that thay vi bi bo qua thanh None.
- Watch-time: ``watchTimeSeconds`` co uu tien tuyet doi; chi dung
  ``estimatedMinutesWatched`` (*60) khi khong co gia tri giay hop le.
  Truoc day phut ghi de giay (120s -> 600s).
- Phat hien video bi xoa: ``_known`` khong con chi song trong 1 instance
  nua. ``run()`` nap ``_known`` tu store (platform_video_id da biet cua
  account) khi bat dau, va store luu append-only nen instance moi cung
  store van phat hien duoc video da bien mat.
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
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").replace("_", "")
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            try:
                f = float(text)
            except ValueError:
                return None
            return int(f) if f.is_integer() else None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: Any) -> Optional[float]:
    """Parse ctr: so, chuoi so ("0.05"), hoac phan tram ("5%" -> 0.05)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        is_percent = text.endswith("%")
        if is_percent:
            text = text[:-1].strip()
        try:
            f = float(text)
        except ValueError:
            return None
        return f / 100.0 if is_percent else f
    return None


def normalize_raw_metric(raw: dict[str, Any], *, video: Video,
                         account: PlatformAccount,
                         collected_at: Optional[datetime] = None,
                         source: Optional[MetricSource] = None) -> UnifiedVideoMetric:
    """Map one raw platform payload onto UnifiedVideoMetric (UTC, seconds).

    Gia tri co trong raw nhung khong parse duoc duoc ghi vao
    ``unparseable`` va lam metric PARTIAL — khong bao gio gan 0 lang le
    voi quality OK. Watch-time: giay (watchTimeSeconds / ms) co uu tien
    tuyet doi, chi dung phut khi khong co giay hop le.
    """
    ts = collected_at or utcnow()
    mapping = RAW_FIELD_MAP[account.platform]
    values: dict[str, Any] = {}
    unparseable: list[str] = []
    missing_required = False
    watch_seconds: Optional[int] = None
    watch_minutes: Optional[int] = None
    watch_ms: Optional[int] = None
    for raw_key, unified in mapping.items():
        if raw_key not in raw:
            continue
        val = _to_int(raw[raw_key])
        if val is None:
            # Truong co mat nhung gia tri hong (vd 'abc'): khong bia 0.
            unparseable.append(raw_key)
            continue
        if unified == "_minutes_watched":
            watch_minutes = val
        elif unified == "_watch_ms":
            watch_ms = val
        elif unified == "watch_time_seconds":
            watch_seconds = val
        else:
            values[unified] = val
    # Uu tien tuyet doi: giay > ms > phut. Phut chi la fallback khi
    # khong co giay hop le (truoc day phut ghi de giay).
    if watch_seconds is not None:
        values["watch_time_seconds"] = watch_seconds
    elif watch_ms is not None:
        values["watch_time_seconds"] = watch_ms // 1000
    elif watch_minutes is not None:
        values["watch_time_seconds"] = watch_minutes * 60
    for name in _REQUIRED_COUNTERS:
        if name not in values:
            missing_required = True
            values.setdefault(name, 0)
    # followers_delta / impressions: hong thi PARTIAL, khong bia 0 lang le.
    followers_raw = raw.get("followers_delta")
    followers_delta = _to_int(followers_raw)
    if followers_raw is not None and followers_delta is None:
        unparseable.append("followers_delta")
        followers_delta = 0
    impressions_raw = raw.get("impressions", raw.get("impression_count"))
    impressions = _to_int(impressions_raw)
    if impressions_raw is not None and impressions is None:
        unparseable.append("impressions")
    ctr: Optional[float] = None
    if "ctr" in raw:
        ctr = _to_float(raw.get("ctr"))
        if ctr is None:
            unparseable.append("ctr")
    quality = (DataQuality.PARTIAL if (missing_required or unparseable)
               else DataQuality.OK)
    return UnifiedVideoMetric(
        video_id=video.id, account_id=account.id, platform=account.platform,
        platform_video_id=video.platform_video_id,
        views=values.get("views", 0), likes=values.get("likes", 0),
        comments=values.get("comments", 0), shares=values.get("shares", 0),
        saves=values.get("saves", 0),
        watch_time_seconds=values.get("watch_time_seconds", 0),
        followers_delta=followers_delta or 0,
        impressions=impressions,
        ctr=ctr,
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

    def known_platform_video_ids(self, account_id: str) -> set[str]:
        """Tat ca platform_video_id da co snapshot cua account nay.

        Day la nguon su that de phat hien video bi xoa: pipeline moi
        instance nao cung nap _known tu day, khong phu thuoc bo nho
        cua instance cu.
        """
        known: set[str] = set()
        for rows in self._by_video.values():
            for m in rows:
                if m.account_id == account_id and m.platform_video_id:
                    known.add(m.platform_video_id)
        return known

    # Alias ngan gon hon cho cung mot nguon su that.
    known_video_ids = known_platform_video_ids

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

    @property
    def known_video_ids(self) -> set[str]:
        """Ban sao tap platform_video_id da biet (tu store + seed)."""
        return set(self._known)

    def _load_known_from_store(self, account: PlatformAccount) -> set[str]:
        """Nap _known tu store/DB (duck-typing de store PG cung dung duoc).

        Thu lan luot cac ten method store co the co; store khong ho tro
        thi tra tap rong va pipeline dung _known trong bo nho.
        """
        for name in ("known_platform_video_ids", "known_video_ids",
                     "list_platform_video_ids"):
            method = getattr(self._store, name, None)
            if callable(method):
                try:
                    return set(method(account.id))
                except TypeError:
                    continue
        return set()

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
        # 0) Nap _known tu store truoc khi so sanh: instance moi cung
        #    store van biet video nao da tung co (de phat hien video xoa).
        self._known = set(self._known) | self._load_known_from_store(account)
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
