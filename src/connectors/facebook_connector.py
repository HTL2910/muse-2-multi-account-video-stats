"""approach_05 — Connector Facebook: Graph API + Page/Video Insights.

Uu tien Facebook Graph API (Meta for Developers). Chi dung thu vien
chuan (urllib) de khong phu thuoc requests. Http layer duoc inject qua
``http_get`` de test duoc ma khong goi that, va de scheduler/worker
chen proxy cua account (1 account = 1 proxy sticky) o tang transport.

Fail-closed: loi xac thuc (OAuth 190/102/200, HTTP 401/403) -> raise
AdapterAuthError, pipeline chuyen account sang NEEDS_REAUTH, khong
retry vo han, khong dung token/proxy cua account khac.
Rate-limit (HTTP 429 / error 4, 17, 32, 613, 80004) -> backoff mu +
Retry-After, toi da MAX_JOB_RETRIES lan.
Moi ban ghi deu ghi ro ``source`` (FACEBOOK_GRAPH_API hoac
BROWSER_FALLBACK) va ``quality`` (OK/PARTIAL khi thieu field).
Khong bao gio log token plaintext — chi log mask.
"""
from __future__ import annotations

import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

try:
    from src.contracts import (  # type: ignore
        MAX_JOB_RETRIES, MetricSource, Platform, PlatformAccount,
        TokenSet, UnifiedVideoMetric, Video, VideoStatus,
        DataQuality, utcnow,
    )
    from src.core.models import AdapterAuthError, AdapterRateLimitError, PlatformAdapterBase  # type: ignore
except ImportError:
    from ..contracts import (  # type: ignore
        MAX_JOB_RETRIES, MetricSource, Platform, PlatformAccount,
        TokenSet, UnifiedVideoMetric, Video, VideoStatus,
        DataQuality, utcnow,
    )
    from ..core.models import AdapterAuthError, AdapterRateLimitError, PlatformAdapterBase  # type: ignore

__all__ = [
    "FacebookConnector", "FacebookApiError", "GraphResponse",
    "GRAPH_API_VERSION", "GRAPH_BASE_URL",
    "parse_facebook_video", "parse_facebook_insights",
]

GRAPH_API_VERSION = "v19.0"
GRAPH_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Ma loi Graph API: xac thuc / rate-limit (theo tai lieu Meta).
AUTH_ERROR_CODES = {102, 190, 200, 2500}
RATE_LIMIT_CODES = {4, 17, 32, 613, 80004}
# Insights can lay: views, reach, watch-time. Like/comment/share lay o node video.
INSIGHT_METRICS = "post_video_views,post_video_view_time,post_impressions,post_video_views_unique"
VIDEO_FIELDS = (
    "id,title,description,created_time,permalink_url,length,"
    "views,likes.summary(true),comments.summary(true),shares"
)


class FacebookApiError(RuntimeError):
    def __init__(self, message: str, *, status: Optional[int] = None,
                 code: Optional[int] = None, retry_after: Optional[float] = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after


@dataclass
class GraphResponse:
    data: Any
    paging: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_facebook_video(raw: dict[str, Any], *, account: PlatformAccount,
                         video_id_map: Optional[dict[str, str]] = None) -> Video:
    """Map video goc Graph API -> Video chuan. ``video_id_map``: platform_video_id -> UUID noi bo."""
    pid = str(raw.get("id", ""))
    internal = (video_id_map or {}).get(pid) or f"fb-{pid}"
    return Video(
        id=internal, account_id=account.id, platform=Platform.FACEBOOK,
        platform_video_id=pid, title=str(raw.get("title") or raw.get("description") or ""),
        description=str(raw.get("description") or ""),
        url=str(raw.get("permalink_url") or f"https://www.facebook.com/watch/?v={pid}"),
        thumbnail_url=str((raw.get("thumbnails") or {}).get("data", [{}])[0].get("uri", "")) if isinstance(raw.get("thumbnails"), dict) else "",
        duration_seconds=_to_int(raw.get("length")) or None,
        published_at=_parse_dt(raw.get("created_time")),
        status=VideoStatus.ACTIVE,
    )


def parse_facebook_insights(raw_insights: list[dict[str, Any]], base: dict[str, Any],
                            *, video: Video, account: PlatformAccount,
                            source: MetricSource = MetricSource.FACEBOOK_GRAPH_API) -> UnifiedVideoMetric:
    """Map insights + node video -> UnifiedVideoMetric. Thieu field = PARTIAL, khong dien 0 gia."""
    by_name = {i.get("name"): i for i in raw_insights}
    def _val(name: str) -> int:
        vals = (by_name.get(name) or {}).get("values") or []
        return _to_int(vals[-1].get("value")) if vals else 0
    views = _val("post_video_views") or _to_int(base.get("views"))
    watch = _val("post_video_view_time")
    impressions = _val("post_impressions") or None
    likes = _to_int((base.get("likes") or {}).get("summary", {}).get("total_count"))
    comments = _to_int((base.get("comments") or {}).get("summary", {}).get("total_count"))
    shares = _to_int((base.get("shares") or {}).get("count"))
    missing = [k for k, v in {"views": views, "likes": likes}.items() if v == 0 and k not in base]
    return UnifiedVideoMetric(
        video_id=video.id, account_id=account.id, platform=Platform.FACEBOOK,
        platform_video_id=video.platform_video_id, views=views, likes=likes,
        comments=comments, shares=shares, watch_time_seconds=watch,
        impressions=impressions, source=source,
        quality=DataQuality.PARTIAL if missing else DataQuality.OK,
    )


class FacebookConnector(PlatformAdapterBase):
    """Adapter Facebook. ``token_resolver`` tra token tu Vault (chi o worker).

    ``http_get(url, headers)`` mac dinh dung urllib; test inject fake.
    ``fallback_fetch`` (optional): lay metric cong khai qua browser profile
    cua chinh account do khi API thieu field — bi gioi han tan suat boi
    ``fallback_max_per_day`` va luon ghi source=BROWSER_FALLBACK.
    """

    platform = Platform.FACEBOOK

    def __init__(self, *, token_resolver: Callable[[PlatformAccount], TokenSet],
                 http_get: Optional[Callable[[str, dict[str, str]], dict[str, Any]]] = None,
                 fallback_fetch: Optional[Callable[[PlatformAccount, Video], dict[str, Any]]] = None,
                 fallback_max_per_day: int = 20,
                 max_retries: int = MAX_JOB_RETRIES,
                 rng: Optional[random.Random] = None,
                 app_id: Optional[str] = None,
                 app_secret: Optional[str] = None):
        self._token_resolver = token_resolver
        self._http_get = http_get or self._default_http_get
        self._fallback_fetch = fallback_fetch
        self._fallback_max_per_day = fallback_max_per_day
        self._fallback_used_today = 0
        self._app_id = app_id
        self._app_secret = app_secret
        self.max_retries = max_retries
        self._rng = rng or random.Random()

    # -- transport mac dinh (urllib, timeout 30s, khong log token) ---------
    @staticmethod
    def _default_http_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                body = {}
            err = (body.get("error") or {})
            raise FacebookApiError(
                f"Graph API HTTP {exc.code}: {err.get('message', 'unknown')}",
                status=exc.code, code=_to_int(err.get("code")),
                retry_after=float(exc.headers.get("Retry-After", 0) or 0) or None,
            ) from exc

    def _call(self, account: PlatformAccount, path: str, params: dict[str, Any]) -> GraphResponse:
        self.guard(account)
        tokens = self._token_resolver(account)
        if not tokens.access_token:
            raise AdapterAuthError(f"account {account.id} has no access_token")
        query = {"access_token": tokens.access_token, **{k: v for k, v in params.items() if v is not None}}
        url = f"{GRAPH_BASE_URL}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
        # Token chi nam trong query cua request Graph API, khong log url day du.
        last: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                body = self._http_get(url, {"Accept": "application/json"})
                if "error" in body:
                    err = body["error"]
                    raise FacebookApiError(str(err.get("message")), code=_to_int(err.get("code")))
                return GraphResponse(data=body.get("data", body), paging=body.get("paging", {}), raw=body)
            except FacebookApiError as exc:
                last = exc
                if exc.code in AUTH_ERROR_CODES or exc.status in (401, 403):
                    raise AdapterAuthError(f"facebook auth failed code={exc.code}: {exc}") from exc
                if exc.code in RATE_LIMIT_CODES or exc.status == 429:
                    if attempt >= self.max_retries:
                        raise AdapterRateLimitError(str(exc), retry_after_seconds=exc.retry_after) from exc
                    wait = (exc.retry_after or (2 ** attempt)) + self._rng.uniform(0, 1)
                    time.sleep(wait)
                    continue
                raise
        raise FacebookApiError(str(last) if last else "unknown Graph API error")

    # -- PlatformAdapter ----------------------------------------------------
    def authenticate(self, account: PlatformAccount) -> TokenSet:
        """Doi token ngan han -> token Page dai han (60 ngay) qua fb_exchange_token."""
        self.guard(account)
        tokens = self._token_resolver(account)
        if not tokens.access_token:
            raise AdapterAuthError(f"account {account.id} has no access_token")
        if not self._app_id or not self._app_secret:
            raise AdapterAuthError("facebook app credentials not configured (pass app_id/app_secret from Vault)")
        resp = self._call(account, "oauth/access_token", {
            "grant_type": "fb_exchange_token", "client_id": self._app_id,
            "client_secret": self._app_secret, "fb_exchange_token": tokens.access_token,
        })
        data = resp.raw if isinstance(resp.raw, dict) else {}
        # _call tra raw body; neu la dict co access_token thi dung, neu khong giu token cu.
        new_token = data.get("access_token") or tokens.access_token
        return TokenSet(access_token=new_token, refresh_token=tokens.refresh_token,
                        expires_at=utcnow().replace(microsecond=0))

    def check_alive(self, account: PlatformAccount) -> bool:
        try:
            resp = self._call(account, "me", {"fields": "id,name"})
            return bool((resp.raw or {}).get("id") or resp.data)
        except AdapterAuthError:
            return False

    def list_videos(self, account: PlatformAccount, *, limit: int = 50) -> list[Video]:
        """GET /{page-id}/videos, phan trang bang cursor ``after``."""
        self.guard(account)
        page_id = account.platform_user_id
        videos: list[Video] = []
        after: Optional[str] = None
        while len(videos) < limit:
            resp = self._call(account, f"{page_id}/videos", {
                "fields": VIDEO_FIELDS, "limit": min(50, limit - len(videos)),
                "after": after,
            })
            items = resp.data if isinstance(resp.data, list) else []
            for raw in items:
                videos.append(parse_facebook_video(raw, account=account))
            after = (resp.paging.get("cursors") or {}).get("after")
            if not after or not items:
                break
        return videos[:limit]

    def get_video_stats(self, account: PlatformAccount, video: Video) -> UnifiedVideoMetric:
        self.guard(account)
        resp = self._call(account, video.platform_video_id, {"fields": VIDEO_FIELDS})
        base = resp.raw if isinstance(resp.raw, dict) else {}
        insights: list[dict[str, Any]] = []
        try:
            ins = self._call(account, f"{video.platform_video_id}/video_insights",
                             {"metric": INSIGHT_METRICS, "period": "lifetime"})
            insights = ins.data if isinstance(ins.data, list) else []
        except (FacebookApiError, AdapterRateLimitError):
            insights = []  # thieu insights -> PARTIAL, khong fail ca metric
        metric = parse_facebook_insights(insights, base, video=video, account=account)
        if metric.quality is DataQuality.PARTIAL and self._fallback_fetch is not None \
                and self._fallback_used_today < self._fallback_max_per_day:
            self._fallback_used_today += 1
            fb = self._fallback_fetch(account, video)
            if fb:
                merged = parse_facebook_insights([], fb, video=video, account=account,
                                                 source=MetricSource.BROWSER_FALLBACK)
                # Chi lay field API thieu, giu nguyen field API da co.
                return UnifiedVideoMetric(
                    video_id=video.id, account_id=account.id, platform=Platform.FACEBOOK,
                    platform_video_id=video.platform_video_id,
                    views=metric.views or merged.views, likes=metric.likes or merged.likes,
                    comments=metric.comments or merged.comments, shares=metric.shares or merged.shares,
                    watch_time_seconds=metric.watch_time_seconds or merged.watch_time_seconds,
                    source=MetricSource.BROWSER_FALLBACK if merged.views else metric.source,
                    quality=DataQuality.PARTIAL,
                )
        return metric

    def refresh_token(self, account: PlatformAccount, tokens: TokenSet) -> TokenSet:
        if tokens.needs_refresh():
            return self.authenticate(account)
        return tokens
