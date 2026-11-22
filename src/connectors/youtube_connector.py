"""approach_07 - Connector YouTube: Data API v3 + Analytics API.

Tuan thu contracts.PlatformAdapter. Transport tiem vao (callable
``transport(method, params) -> dict``) de test khong can mang/credential
that; prod dung google-api client hoac HTTPS + OAuth token tu Vault.

Quota (don vi chinh thuc Data API v3): search.list=100, videos.list=1,
channels.list=1, playlistItems.list=1, playlists.list=1; Analytics
report tam tinh 1-5. Han muc mac dinh 10_000 units/ngay/project.
Connector tu dem quota, cache, batch id (toi da 50/lan goi videos.list),
va tu choi fail-closed khi vuot quota thay vi goi mu.
"""
from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

try:
    from src.contracts import (
        MetricSource, Platform, PlatformAccount, TokenSet, UnifiedVideoMetric,
        Video, VideoStatus, utcnow, validate_metric, DataQuality,
    )
except ImportError:  # pragma: no cover
    from contracts import (  # type: ignore
        MetricSource, Platform, PlatformAccount, TokenSet, UnifiedVideoMetric,
        Video, VideoStatus, utcnow, validate_metric, DataQuality,
    )

__all__ = ["QuotaExceededError", "YouTubeAuthError", "QuotaTracker",
           "YouTubeConnector", "parse_duration_iso8601",
           "QUOTA_COSTS", "DAILY_QUOTA_DEFAULT"]

QUOTA_COSTS = {"search.list": 100, "videos.list": 1, "channels.list": 1,
               "playlistItems.list": 1, "playlists.list": 1,
               "analytics.reports.query": 5}
DAILY_QUOTA_DEFAULT = 10_000

Transport = Callable[[str, dict], dict]
TokenProvider = Callable[[PlatformAccount], TokenSet]


class QuotaExceededError(RuntimeError):
    pass


class YouTubeAuthError(RuntimeError):
    """401/403 auth: pipeline chuyen account sang NEEDS_REAUTH."""
    pass


def parse_duration_iso8601(s: str | None) -> Optional[int]:
    """PT1H2M3S / PT45S / P1DT2H -> giay. None neu khong parse duoc."""
    if not s or not s.startswith("P"):
        return None
    import re
    m = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", s)
    if not m:
        return None
    d, h, mi, sec = (int(x) if x else 0 for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + sec


def _parse_ts(s: str | None) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _int(v) -> int:
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return 0


@dataclass
class QuotaTracker:
    daily_limit: int = DAILY_QUOTA_DEFAULT
    used: int = 0
    day: str = field(default_factory=lambda: utcnow().date().isoformat())
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _roll(self) -> None:
        today = utcnow().date().isoformat()
        if today != self.day:
            self.day, self.used = today, 0

    def charge(self, method: str, *, units: int = 1) -> int:
        cost = QUOTA_COSTS.get(method, 1) * units
        with self._lock:
            self._roll()
            if self.used + cost > self.daily_limit:
                raise QuotaExceededError(
                    f"quota exceeded: used={self.used} cost={cost} limit={self.daily_limit}")
            self.used += cost
            return self.used

    @property
    def remaining(self) -> int:
        with self._lock:
            self._roll()
            return max(0, self.daily_limit - self.used)

    def should_warn(self, *, threshold: float = 0.8) -> bool:
        return self.daily_limit > 0 and (self.daily_limit - self.remaining) / self.daily_limit >= threshold


class YouTubeConnector:
    """PlatformAdapter cho YouTube. 1 connector phuc vu nhieu Google
    account; moi account tu co token rieng qua token_provider (Vault).
    Nhieu channel tren 1 Google account: channel_id nam o
    account.platform_user_id hoac payload['channel_id'].
    """
    platform = Platform.YOUTUBE

    def __init__(self, transport: Transport,
                 token_provider: TokenProvider | None = None,
                 *, quota: QuotaTracker | None = None,
                 cache_ttl_seconds: float = 300.0,
                 clock: Callable[[], float] = time.monotonic):
        self._t, self._tokens = transport, token_provider
        self.quota = quota or QuotaTracker()
        self._ttl, self._clock = cache_ttl_seconds, clock
        self._cache: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    # -- infra ---------------------------------------------------------
    def _call(self, method: str, params: dict, *, cache: bool = True) -> dict:
        key = hashlib.sha256(f"{method}:{sorted(params.items())}".encode()).hexdigest()
        if cache:
            with self._lock:
                hit = self._cache.get(key)
                if hit and self._clock() - hit[0] < self._ttl:
                    return hit[1]
        self.quota.charge(method)
        try:
            resp = self._t(method, params)
        except Exception as exc:
            code = getattr(exc, "status", None) or getattr(exc, "code", None)
            if code in (401, 403):
                raise YouTubeAuthError(str(exc)) from exc
            raise
        if isinstance(resp, dict) and resp.get("error"):
            err = resp["error"]
            if err.get("code") in (401, 403) or "auth" in str(err.get("status", "")).lower():
                raise YouTubeAuthError(str(err.get("message", "auth error")))
        if cache and isinstance(resp, dict):
            with self._lock:
                self._cache[key] = (self._clock(), resp)
        return resp

    @staticmethod
    def _channel_id(account: PlatformAccount) -> str:
        return str(account.__dict__.get("payload", {}).get("channel_id")
                   if isinstance(account.__dict__.get("payload"), dict) else "") \
            or account.platform_user_id

    # -- PlatformAdapter -------------------------------------------------
    def authenticate(self, account: PlatformAccount) -> TokenSet:
        if self._tokens is None:
            raise YouTubeAuthError("no token_provider configured")
        tokens = self._tokens(account)
        if not tokens.access_token or tokens.is_expired():
            raise YouTubeAuthError("missing/expired access token")
        return tokens

    def check_alive(self, account: PlatformAccount) -> bool:
        try:
            resp = self._call("channels.list",
                              {"part": "id", "id": self._channel_id(account)})
            return bool(resp.get("items"))
        except YouTubeAuthError:
            return False

    def refresh_token(self, account: PlatformAccount, tokens: TokenSet) -> TokenSet:
        # Refresh that nam o TokenManager (vault.py); connector chi xac
        # nhan token moi dung duoc, khong tu giu refresh_token.
        if not tokens.refresh_token:
            raise YouTubeAuthError("no refresh_token - needs reauth")
        return self.authenticate(account)

    def list_channels(self, account: PlatformAccount) -> list[dict]:
        """Nhieu channel tren cung 1 Google account (mine=true)."""
        resp = self._call("channels.list",
                          {"part": "snippet,statistics", "mine": True, "maxResults": 50})
        return list(resp.get("items") or [])

    def _uploads_playlist(self, channel_id: str) -> str:
        resp = self._call("channels.list",
                          {"part": "contentDetails", "id": channel_id})
        items = resp.get("items") or []
        if not items:
            raise YouTubeAuthError(f"channel not found: {channel_id}")
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    def list_videos(self, account: PlatformAccount, *, limit: int = 50) -> list[Video]:
        channel_id = self._channel_id(account)
        uploads = self._uploads_playlist(channel_id)
        video_ids: list[str] = []
        page_token = ""
        while len(video_ids) < limit:
            params = {"part": "snippet,contentDetails", "playlistId": uploads,
                      "maxResults": min(50, limit - len(video_ids))}
            if page_token:
                params["pageToken"] = page_token
            resp = self._call("playlistItems.list", params)
            for it in resp.get("items") or []:
                vid = (it.get("contentDetails") or {}).get("videoId")
                if vid:
                    video_ids.append(vid)
            page_token = resp.get("nextPageToken", "")
            if not page_token:
                break
        videos: list[Video] = []
        # Batch videos.list: toi da 50 id / 1 unit - tiet kiem quota nhat
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            resp = self._call("videos.list",
                              {"part": "snippet,contentDetails,statistics,status",
                               "id": ",".join(batch)})
            for it in resp.get("items") or []:
                videos.append(self._to_video(account, it))
        return videos

    @staticmethod
    def _to_video(account: PlatformAccount, item: dict) -> Video:
        sn = item.get("snippet") or {}
        status = (item.get("status") or {}).get("privacyStatus", "public")
        return Video(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL,
                              f"youtube:{account.id}:{item.get('id','')}")),
            account_id=account.id, platform=Platform.YOUTUBE,
            platform_video_id=str(item.get("id", "")),
            title=str(sn.get("title", "")), description=str(sn.get("description", "")),
            thumbnail_url=str(((sn.get("thumbnails") or {}).get("high") or {}).get("url", "")),
            url=f"https://www.youtube.com/watch?v={item.get('id','')}",
            duration_seconds=parse_duration_iso8601(
                (item.get("contentDetails") or {}).get("duration")),
            published_at=_parse_ts(sn.get("publishedAt")),
            status=VideoStatus.ACTIVE if status == "public" else VideoStatus.PRIVATE)

    def get_video_stats(self, account: PlatformAccount, video: Video) -> UnifiedVideoMetric:
        resp = self._call("videos.list",
                          {"part": "statistics", "id": video.platform_video_id})
        items = resp.get("items") or []
        stats = (items[0].get("statistics") if items else {}) or {}
        metric = UnifiedVideoMetric(
            video_id=video.id, account_id=account.id, platform=Platform.YOUTUBE,
            platform_video_id=video.platform_video_id,
            views=_int(stats.get("viewCount")), likes=_int(stats.get("likeCount")),
            comments=_int(stats.get("commentCount")),
            source=MetricSource.YOUTUBE_DATA_API, collected_at=utcnow())
        return metric

    def get_analytics(self, account: PlatformAccount, video: Video,
                      *, start_date: str, end_date: str) -> dict:
        """Analytics API: watch time, impressions, CTR, subs gained/lost.

        Tra dict chuan hoa; watch_time quy ra giay (API tra phut).
        """
        resp = self._call("analytics.reports.query", {
            "ids": f"channel=={self._channel_id(account)}",
            "startDate": start_date, "endDate": end_date,
            "metrics": "estimatedMinutesWatched,impressions,impressionClickThroughRate,subscribersGained,subscribersLost,views",
            "filters": f"video=={video.platform_video_id}"}, cache=False)
        rows = resp.get("rows") or []
        if not rows:
            return {"watch_time_seconds": 0, "impressions": None, "ctr": None,
                    "subscribers_gained": 0, "subscribers_lost": 0,
                    "followers_delta": 0, "views": 0}
        r = rows[0]
        gained, lost = _int(r[3]), _int(r[4])
        return {"watch_time_seconds": _int(r[0]) * 60,
                "impressions": _int(r[1]),
                "ctr": (float(r[2]) / 100.0) if r[2] is not None else None,
                "subscribers_gained": gained, "subscribers_lost": lost,
                "followers_delta": gained - lost, "views": _int(r[5])}

    def get_video_stats_full(self, account: PlatformAccount, video: Video,
                             *, start_date: str, end_date: str) -> UnifiedVideoMetric:
        """Gop Data API + Analytics thanh 1 UnifiedVideoMetric."""
        base = self.get_video_stats(account, video)
        a = self.get_analytics(account, video, start_date=start_date, end_date=end_date)
        metric = UnifiedVideoMetric(
            video_id=base.video_id, account_id=base.account_id,
            platform=Platform.YOUTUBE, platform_video_id=base.platform_video_id,
            views=base.views, likes=base.likes, comments=base.comments,
            watch_time_seconds=a["watch_time_seconds"],
            followers_delta=a["followers_delta"], impressions=a["impressions"],
            ctr=a["ctr"], source=MetricSource.YOUTUBE_ANALYTICS_API,
            collected_at=utcnow(),
            quality=DataQuality.NEEDS_REVIEW if validate_metric(base) else DataQuality.OK)
        return metric

    @staticmethod
    def priority_score(video: Video, latest_views: int,
                       previous_views: int) -> float:
        """Uu tien lap lich: video moi + dang tang truong diem cao hon."""
        score = 0.0
        if video.published_at:
            age_days = max(0.0, (utcnow() - video.published_at).total_seconds() / 86400)
            score += max(0.0, 30.0 - age_days) / 30.0
        if previous_views > 0 and latest_views > previous_views:
            score += min(1.0, (latest_views - previous_views) / previous_views)
        return round(score, 4)
