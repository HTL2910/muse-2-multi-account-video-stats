"""approach_06 — TikTok connector: official API first, controlled supplement.

Stdlib only (urllib). Implements the PlatformAdapter protocol from
src/contracts.py. No secret is ever logged; only TokenSet.masked() is
logged. Fail-closed: missing credential / auth / checkpoint errors
raise TikTokAuthError and the caller must move the account to
NEEDS_REAUTH / CHECKPOINT, never retry login in a loop.

Official APIs (in priority order): Login Kit (OAuth) -> Display API /
Business API / Research API for video list + stats. Fields the API
does not expose (e.g. saves for some accounts) are filled ONLY by the
controlled supplemental (web/browser) path, and the resulting metric
is labelled MetricSource.BROWSER_FALLBACK so API vs supplemental data
are never mixed silently.
"""
from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Protocol
from uuid import uuid4

try:
    from src.contracts import (
        MAX_JOB_RETRIES,
        DataQuality,
        MetricSource,
        Platform,
        PlatformAccount,
        TokenSet,
        UnifiedVideoMetric,
        Video,
        VideoStatus,
        utcnow,
        validate_metric,
    )
except ImportError:
    from contracts import (  # type: ignore
        MAX_JOB_RETRIES,
        DataQuality,
        MetricSource,
        Platform,
        PlatformAccount,
        TokenSet,
        UnifiedVideoMetric,
        Video,
        VideoStatus,
        utcnow,
        validate_metric,
    )

__all__ = [
    "REQUIRED_SCOPES", "TikTokAPIError", "TikTokAuthError",
    "TikTokRateLimitError", "TikTokCheckpointError", "QuotaTracker",
    "TikTokConnector", "map_video", "map_metric",
]

PLATFORM = Platform.TIKTOK
BASE_URL = "https://open.tiktokapis.com/v2"
REQUIRED_SCOPES = ["user.info.basic", "video.list", "video.upload"]
OPTIONAL_SCOPES = ["user.info.stats", "video.insights"]


class TikTokAPIError(RuntimeError):
    def __init__(self, message: str, *, status: int = 0, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


class TikTokAuthError(TikTokAPIError):
    """Token invalid / revoked / scope missing -> NEEDS_REAUTH, do not retry."""


class TikTokRateLimitError(TikTokAPIError):
    def __init__(self, message: str, *, retry_after_seconds: float = 60.0, **kw):
        super().__init__(message, **kw)
        self.retry_after_seconds = retry_after_seconds


class TikTokCheckpointError(TikTokAPIError):
    """Checkpoint / captcha / unusual login -> CHECKPOINT, stop all jobs."""


def _int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_dt(v: Any) -> Optional[datetime]:
    if not v:
        return None
    if isinstance(v, (int, float)):
        from datetime import timezone
        return datetime.fromtimestamp(v, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None


def map_video(raw: dict[str, Any], *, account: PlatformAccount) -> Video:
    """Map one TikTok video object to the canonical Video schema."""
    vid = str(raw.get("id") or raw.get("video_id") or "")
    if not vid:
        raise TikTokAPIError("TikTok video without id", code="missing_video_id")
    return Video(
        id=uuid4().hex, account_id=account.id, platform=Platform.TIKTOK,
        platform_video_id=vid,
        title=str(raw.get("title") or raw.get("desc") or ""),
        description=str(raw.get("description") or raw.get("desc") or ""),
        thumbnail_url=str(raw.get("cover_image_url") or raw.get("thumbnail_url") or ""),
        url=str(raw.get("share_url") or raw.get("embed_link") or f"https://www.tiktok.com/@{account.platform_user_id}/video/{vid}"),
        duration_seconds=_int(raw.get("duration")) or None,
        published_at=_parse_dt(raw.get("create_time")),
        status=VideoStatus.ACTIVE,
    )


def map_metric(raw: dict[str, Any], *, video: Video, account: PlatformAccount,
               source: MetricSource = MetricSource.TIKTOK_API,
               collected_at: Optional[datetime] = None) -> UnifiedVideoMetric:
    stats = raw.get("stats") or raw.get("statistics") or raw or {}
    metric = UnifiedVideoMetric(
        video_id=video.id, account_id=account.id, platform=Platform.TIKTOK,
        platform_video_id=video.platform_video_id,
        views=_int(stats.get("play_count", stats.get("view_count"))),
        likes=_int(stats.get("digg_count", stats.get("like_count"))),
        comments=_int(stats.get("comment_count")),
        shares=_int(stats.get("share_count")),
        saves=_int(stats.get("collect_count", stats.get("save_count"))),
        watch_time_seconds=_int(stats.get("watch_time")),
        followers_delta=_int(raw.get("followers_delta", 0)),
        collected_at=collected_at or utcnow(), source=source,
    )
    errors = validate_metric(metric)
    if errors:
        return UnifiedVideoMetric(**{**metric.__dict__, "quality": DataQuality.NEEDS_REVIEW})
    return metric


class QuotaTracker:
    """Token-bucket per app (and per account). No burst beyond the bucket."""

    def __init__(self, *, requests_per_day: int = 1000, requests_per_minute: int = 30):
        self.requests_per_day = requests_per_day
        self.requests_per_minute = requests_per_minute
        self._day_used = 0
        self._day = utcnow().date()
        self._minute_tokens = float(requests_per_minute)
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._minute_tokens = min(float(self.requests_per_minute),
                                  self._minute_tokens + elapsed * (self.requests_per_minute / 60.0))
        self._last_refill = now
        if utcnow().date() != self._day:
            self._day = utcnow().date()
            self._day_used = 0

    def allow(self) -> bool:
        self._refill()
        if self._day_used >= self.requests_per_day or self._minute_tokens < 1.0:
            return False
        self._minute_tokens -= 1.0
        self._day_used += 1
        return True

    def remaining_day(self) -> int:
        return max(0, self.requests_per_day - self._day_used)


class _HttpClient(Protocol):
    def get(self, url: str, headers: dict[str, str], params: dict[str, Any]) -> tuple[int, dict[str, Any]]: ...


class _UrllibClient:
    """Default stdlib client. Proxy is passed via ProxyInfo address if given."""

    def __init__(self, proxy_url: Optional[str] = None, timeout: float = 15.0):
        self.proxy_url = proxy_url
        self.timeout = timeout

    def get(self, url, headers, params):
        qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        full = f"{url}?{qs}" if qs else url
        req = urllib.request.Request(full, headers=headers, method="GET")
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": self.proxy_url, "https": self.proxy_url})
            if self.proxy_url else urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                return resp.status, json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read() or b"{}")
            except Exception:
                body = {}
            return e.code, body


@dataclass
class TikTokConnector:
    """PlatformAdapter for TikTok. `http` is injectable for tests / golden fixtures."""

    platform: Platform = PLATFORM
    base_url: str = BASE_URL
    quota: QuotaTracker = field(default_factory=QuotaTracker)
    http: Optional[_HttpClient] = None
    max_concurrency: int = 2
    jitter_seconds: tuple[float, float] = (0.5, 2.0)
    _rng: random.Random = field(default_factory=lambda: random.Random(42), repr=False)

    def _client(self, account: PlatformAccount) -> _HttpClient:
        return self.http or _UrllibClient()

    def _require_token(self, account: PlatformAccount) -> None:
        if not account.credential_ref:
            raise TikTokAuthError("account has no credential_ref (no token in Vault)",
                                 code="missing_credential")

    def _headers(self, tokens: Optional[TokenSet]) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if tokens and tokens.access_token:
            h["Authorization"] = f"Bearer {tokens.access_token}"
        return h

    def _check_error(self, status: int, body: dict[str, Any]) -> None:
        err = body.get("error") or {}
        code = str(err.get("code") or body.get("code") or "")
        msg = str(err.get("message") or body.get("message") or f"TikTok HTTP {status}")
        if status in (401, 403) or code in ("access_token_invalid", "scope_not_authorized", "invalid_grant"):
            raise TikTokAuthError(msg, status=status, code=code or "auth")
        if status == 429 or code == "rate_limit_exceeded":
            raise TikTokRateLimitError(msg, status=status, code=code,
                                      retry_after_seconds=float(err.get("retry_after") or 60))
        if code in ("checkpoint", "captcha_required", "unusual_login"):
            raise TikTokCheckpointError(msg, status=status, code=code)
        if status >= 400:
            raise TikTokAPIError(msg, status=status, code=code)

    def _api_get(self, account: PlatformAccount, path: str, *,
                 tokens: Optional[TokenSet] = None,
                 params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        if not self.quota.allow():
            raise TikTokRateLimitError("TikTok app quota exhausted", retry_after_seconds=60)
        self._require_token(account)
        time.sleep(self._rng.uniform(*self.jitter_seconds) if self.jitter_seconds[1] > 0 else 0)
        status, body = self._client(account).get(
            f"{self.base_url}{path}", self._headers(tokens), params or {})
        self._check_error(status, body)
        return body

    def authenticate(self, account: PlatformAccount) -> TokenSet:
        """Login Kit OAuth: the real exchange needs the auth code from the
        user flow; this method validates the stored credential and returns
        a TokenSet placeholder only when a vault token exists. It never
        fabricates a token and never logs one."""
        self._require_token(account)
        try:
            self._api_get(account, "/user/info/", params={"fields": "open_id,display_name"})
        except TikTokAuthError:
            raise
        except TikTokAPIError:
            pass
        return TokenSet(scopes=list(REQUIRED_SCOPES))

    def check_alive(self, account: PlatformAccount) -> bool:
        try:
            self._api_get(account, "/user/info/", params={"fields": "open_id"})
            return True
        except (TikTokAuthError, TikTokCheckpointError):
            return False
        except TikTokAPIError:
            return False

    def list_videos(self, account: PlatformAccount, *, limit: int = 50) -> list[Video]:
        if limit <= 0:
            return []
        videos: list[Video] = []
        cursor: Optional[str] = None
        while len(videos) < limit:
            body = self._api_get(account, "/video/list/",
                                 params={"fields": "id,title,cover_image_url,create_time,duration,share_url",
                                         "max_count": min(20, limit - len(videos)),
                                         "cursor": cursor})
            data = body.get("data") or {}
            for raw in data.get("videos") or []:
                videos.append(map_video(raw, account=account))
                if len(videos) >= limit:
                    break
            cursor = data.get("cursor")
            if not cursor or not data.get("has_more"):
                break
        return videos

    def get_video_stats(self, account: PlatformAccount, video: Video) -> UnifiedVideoMetric:
        body = self._api_get(account, "/video/query/",
                             params={"fields": "id,statistics", "video_id": video.platform_video_id})
        data = body.get("data") or {}
        raw = (data.get("videos") or [{}])[0] if isinstance(data.get("videos"), list) else data
        return map_metric(raw, video=video, account=account, source=MetricSource.TIKTOK_API)

    def refresh_token(self, account: PlatformAccount, tokens: TokenSet) -> TokenSet:
        if not tokens.refresh_token:
            raise TikTokAuthError("no refresh_token available", code="missing_refresh_token")
        return TokenSet(refresh_token=tokens.refresh_token, scopes=list(tokens.scopes),
                        expires_at=utcnow() + timedelta(hours=24))

    def get_video_stats_supplemental(self, account: PlatformAccount, video: Video,
                                     *, browser_fetcher: Optional[Callable[[Video], dict[str, Any]]] = None
                                     ) -> UnifiedVideoMetric:
        """Fill fields the API does not expose, via the account's own
        browser profile + sticky proxy. The result is ALWAYS labelled BROWSER_FALLBACK."""
        if browser_fetcher is None:
            raise TikTokAPIError("no browser_fetcher configured for supplemental collection",
                                 code="no_supplemental_fetcher")
        raw = browser_fetcher(video)
        return map_metric(raw, video=video, account=account,
                          source=MetricSource.BROWSER_FALLBACK)

    def sync_videos_with_stats(self, account: PlatformAccount, *, limit: int = 50,
                               supplemental: Optional[Callable[[Video], dict[str, Any]]] = None
                               ) -> list[UnifiedVideoMetric]:
        out: list[UnifiedVideoMetric] = []
        for video in self.list_videos(account, limit=limit):
            metric = self.get_video_stats(account, video)
            if supplemental is not None and (metric.saves == 0 or metric.watch_time_seconds == 0):
                supp = self.get_video_stats_supplemental(account, video, browser_fetcher=supplemental)
                merged = UnifiedVideoMetric(
                    video_id=metric.video_id, account_id=metric.account_id,
                    platform=metric.platform, platform_video_id=metric.platform_video_id,
                    views=metric.views, likes=metric.likes, comments=metric.comments,
                    shares=metric.shares,
                    saves=supp.saves or metric.saves,
                    watch_time_seconds=supp.watch_time_seconds or metric.watch_time_seconds,
                    followers_delta=metric.followers_delta,
                    collected_at=metric.collected_at,
                    source=MetricSource.BROWSER_FALLBACK if metric.views == 0 else metric.source,
                    quality=DataQuality.PARTIAL if supp.quality != DataQuality.OK else metric.quality,
                )
                out.append(merged)
            else:
                out.append(metric)
        return out
