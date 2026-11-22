"""approach_07 - Connector YouTube: Data API v3 + Analytics API.

Tuan thu contracts.PlatformAdapter. Transport tiem vao (callable
``transport(method, params) -> dict``) de test khong can mang/credential
that; prod dung google-api client hoac HTTPS + OAuth token tu Vault.

Quota (don vi chinh thuc Data API v3): search.list=100, videos.list=1,
channels.list=1, playlistItems.list=1, playlists.list=1; Analytics
report tam tinh 1-5. Han muc mac dinh 10_000 units/ngay/project.
Connector tu dem quota, cache, batch id (toi da 50/lan goi videos.list),
va tu choi fail-closed khi vuot quota thay vi goi mu.

Fix ``fix_youtube_quota_cache_channel`` (gop
fix_youtube_quota_per_instance_and_chann +
fix_youtube_cache_stale_and_mutable):
- Quota la ngan sach cua GOOGLE CLOUD PROJECT, khong phai cua tung
  connector: QuotaTracker nay chi la mat tien (facade) tren mot
  QuotaStore DUNG CHUNG theo project_id — increment nguyen tu
  (check-and-add trong cung mot thao tac khoa/Lua), khoa theo ngay
  UTC cua project nen tu roll sang ngay moi. Backend san co:
  InMemoryQuotaStore (instance dung chung cap process — mac dinh,
  chi dung duoc khi moi worker o cung process) va RedisQuotaStore
  (INCRBY + kiem tra gioi han bang Lua, production nhieu worker).
  Khong truy cap duoc store quota thi charge() raise
  QuotaExceededError — FAIL-CLOSED, khong goi API mu.
- Cache: tra ve ban DEEPCOPY ca khi ghi va khi doc — caller sua dict
  nhan duoc khong lam hong cache, cache cung khong tra cung mot dict
  mutable cho hai caller. videos.list co part statistics KHONG duoc
  cache (so lieu thay doi lien tuc; snapshot moi khong bao gio mang
  so cu 300s nua); chi cache metadata it doi (channels.list,
  videos.list khong statistics, playlistItems.list) voi TTL rieng
  theo loai du lieu. Cache key chua project_id + account_id + method
  + params: hai account khac nhau khong bao gio dung chung mot entry
  (list_channels mine=true truoc day tra nham channel cua account
  khac vi key khong co account).
- channel_id: truoc day doc account.__dict__['payload'] — PlatformAccount
  khong co field payload nen do la DEAD CODE, luc nao cung roi ve
  platform_user_id. Nay thu tu ro rang: (1) bang mapping
  account->channel cua connector (register_channel / channel_map),
  (2) field account.channel_id neu contracts co khai bao, (3)
  account.platform_user_id.
"""
from __future__ import annotations

import copy
import hashlib
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

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
           "QUOTA_COSTS", "DAILY_QUOTA_DEFAULT",
           "QuotaStore", "InMemoryQuotaStore", "RedisQuotaStore"]

QUOTA_COSTS = {"search.list": 100, "videos.list": 1, "channels.list": 1,
               "playlistItems.list": 1, "playlists.list": 1,
               "analytics.reports.query": 5}
DAILY_QUOTA_DEFAULT = 10_000

# TTL cache theo LOAI du lieu (giay). Chi metadata it doi moi duoc
# cache; du lieu statistics/analytics khong bao gio vao bang nay.
CACHE_TTL_METADATA = 3600.0     # channels.list: thong tin kenh it doi
CACHE_TTL_LISTING = 300.0       # videos.list (khong statistics), playlistItems.list

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


def _utc_day() -> str:
    return utcnow().date().isoformat()


def _default_project_id() -> str:
    return (os.environ.get("GOOGLE_CLOUD_PROJECT")
            or os.environ.get("YOUTUBE_PROJECT_ID") or "default")


# ---------------------------------------------------------------- Quota store
class QuotaStore(Protocol):
    """Bo dem quota DUNG CHUNG theo project. add() phai nguyen tu:
    cong cost va kiem tra gioi han trong cung mot thao tac."""

    def add(self, project_id: str, day: str, cost: int, limit: int) -> int:
        """Tra tong used moi; raise QuotaExceededError neu vuot limit
        (va KHONG cong). Loi ha tang (mat ket noi...) thi raise len —
        QuotaTracker se chuyen thanh QuotaExceededError fail-closed."""
        ...

    def used(self, project_id: str, day: str) -> int: ...


class InMemoryQuotaStore:
    """QuotaStore trong RAM. Mot instance DUNG CHUNG (mac dinh la
    singleton cap process ben duoi) cho moi QuotaTracker cung
    project_id — cac connector trong cung process chia se mot ngan
    sach. Nhieu process thi phai dung RedisQuotaStore."""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def add(self, project_id: str, day: str, cost: int, limit: int) -> int:
        with self._lock:
            key = (project_id, day)
            cur = self._data.get(key, 0)
            if cur + cost > limit:
                raise QuotaExceededError(
                    f"quota exceeded: project={project_id} used={cur} "
                    f"cost={cost} limit={limit}")
            self._data[key] = cur + cost
            # Don khoa ngay cu cua cung project (roll theo ngay UTC)
            for k in [k for k in self._data if k[0] == project_id and k[1] != day]:
                del self._data[k]
            return cur + cost

    def used(self, project_id: str, day: str) -> int:
        with self._lock:
            return self._data.get((project_id, day), 0)


class RedisQuotaStore:
    """QuotaStore tren Redis: Lua script INCRBY + kiem tra gioi han
    nguyen tu trong mot round-trip, key chua ngay UTC nen tu roll;
    EXPIRE 48h de key tu don. ``client`` la redis-py (eval)."""

    _ADD_LUA = (
        "local used = redis.call('INCRBY', KEYS[1], ARGV[1]); "
        "if used > tonumber(ARGV[2]) then "
        "redis.call('INCRBY', KEYS[1], -ARGV[1]); return -1 end; "
        "redis.call('EXPIRE', KEYS[1], ARGV[3]); return used")

    def __init__(self, client, *, prefix: str = "yt-quota:"):
        self._client = client
        self._prefix = prefix

    def _key(self, project_id: str, day: str) -> str:
        return f"{self._prefix}{project_id}:{day}"

    def add(self, project_id: str, day: str, cost: int, limit: int) -> int:
        used = int(self._client.eval(self._ADD_LUA, 1, self._key(project_id, day),
                                     cost, limit, 48 * 3600))
        if used < 0:
            raise QuotaExceededError(
                f"quota exceeded: project={project_id} cost={cost} limit={limit}")
        return used

    def used(self, project_id: str, day: str) -> int:
        raw = self._client.get(self._key(project_id, day))
        return int(raw) if raw is not None else 0


_DEFAULT_QUOTA_STORE = InMemoryQuotaStore()


class QuotaTracker:
    """Mat tien dem quota theo PROJECT (khong con la bo dem rieng cua
    tung connector). Moi tracker cung project_id + cung store thi
    chia se mot ngan sach 10k units/ngay; mac dinh store la singleton
    in-memory cap process, production truyen RedisQuotaStore.

    ``now_fn`` tiem vao de test roll ngay; mac dinh la utcnow cua
    contracts (ngay UTC).
    """

    def __init__(self, daily_limit: int = DAILY_QUOTA_DEFAULT,
                 *, project_id: Optional[str] = None,
                 store: Optional[QuotaStore] = None,
                 now_fn: Optional[Callable[[], datetime]] = None):
        self.daily_limit = int(daily_limit)
        self.project_id = project_id or _default_project_id()
        self._store: QuotaStore = store or _DEFAULT_QUOTA_STORE
        self._now_fn = now_fn or utcnow

    def _today(self) -> str:
        return self._now_fn().date().isoformat()

    @property
    def day(self) -> str:
        return self._today()

    @property
    def used(self) -> int:
        try:
            return self._store.used(self.project_id, self._today())
        except Exception as exc:
            raise QuotaExceededError(
                f"quota store unavailable (fail-closed): {exc}") from exc

    def charge(self, method: str, *, units: int = 1) -> int:
        cost = QUOTA_COSTS.get(method, 1) * units
        try:
            return self._store.add(self.project_id, self._today(),
                                   cost, self.daily_limit)
        except QuotaExceededError:
            raise
        except Exception as exc:  # store loi/mat ket noi: FAIL-CLOSED
            raise QuotaExceededError(
                f"quota store unavailable (fail-closed): {exc}") from exc

    @property
    def remaining(self) -> int:
        return max(0, self.daily_limit - self.used)

    def should_warn(self, *, threshold: float = 0.8) -> bool:
        return self.daily_limit > 0 and (self.daily_limit - self.remaining) / self.daily_limit >= threshold


class YouTubeConnector:
    """PlatformAdapter cho YouTube. 1 connector phuc vu nhieu Google
    account; moi account tu co token rieng qua token_provider (Vault).
    Nhieu channel tren 1 Google account: channel_id lay theo thu tu
    mapping cua connector (register_channel) -> field channel_id cua
    account (neu contracts co) -> account.platform_user_id.
    """
    platform = Platform.YOUTUBE

    def __init__(self, transport: Transport,
                 token_provider: TokenProvider | None = None,
                 *, quota: QuotaTracker | None = None,
                 project_id: Optional[str] = None,
                 channel_map: Optional[dict[str, str]] = None,
                 cache_ttl_seconds: float = CACHE_TTL_LISTING,
                 clock: Callable[[], float] = time.monotonic):
        self._t, self._tokens = transport, token_provider
        self.project_id = project_id or (quota.project_id if quota else _default_project_id())
        self.quota = quota or QuotaTracker(project_id=self.project_id)
        self._channel_map: dict[str, str] = dict(channel_map or {})
        self._ttl, self._clock = cache_ttl_seconds, clock
        self._cache: dict[str, tuple[float, float, dict]] = {}
        self._lock = threading.Lock()

    # -- channel mapping -------------------------------------------------
    def register_channel(self, account_id: str, channel_id: str) -> None:
        """Gan account -> channel ro rang (bang mapping rieng cua
        connector, dung khi 1 Google account co nhieu channel)."""
        self._channel_map[str(account_id)] = str(channel_id)

    def _channel_id(self, account: PlatformAccount) -> str:
        mapped = self._channel_map.get(str(account.id))
        if mapped:
            return mapped
        explicit = getattr(account, "channel_id", "")  # field contracts neu co
        if explicit:
            return str(explicit)
        return account.platform_user_id

    # -- infra ---------------------------------------------------------
    def _cache_policy(self, method: str, params: dict) -> Optional[float]:
        """Tra TTL neu (method, params) duoc phep cache, None neu khong.

        Nguyen tac: chi metadata it doi moi duoc cache. videos.list
        kem statistics va analytics/search KHONG bao gio duoc cache —
        so lieu moi phai la so moi.
        """
        part = str(params.get("part", ""))
        if method == "channels.list":
            return CACHE_TTL_METADATA
        if method == "videos.list":
            if "statistics" in part:
                return None
            return self._ttl
        if method == "playlistItems.list":
            return self._ttl
        return None

    def _call(self, method: str, params: dict, *, cache: Optional[bool] = None,
              account_id: Optional[str] = None) -> dict:
        # cache=False -> khong cache; cac truong hop con lai theo
        # policy — ke ca caller truyen cache=True cung khong ep duoc
        # statistics vao cache.
        ttl = None if cache is False else self._cache_policy(method, params)
        key_src = (f"{self.project_id}:{account_id or ''}:{method}:"
                   f"{sorted(params.items())}")
        key = hashlib.sha256(key_src.encode()).hexdigest()
        if ttl is not None:
            with self._lock:
                hit = self._cache.get(key)
                if hit and self._clock() - hit[0] < hit[1]:
                    return copy.deepcopy(hit[2])  # khong tra dict mutable dung chung
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
        if ttl is not None and isinstance(resp, dict):
            with self._lock:
                # Luu ban deepcopy: caller sua dict tra ve khong hong cache
                self._cache[key] = (self._clock(), ttl, copy.deepcopy(resp))
        return resp

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
                              {"part": "id", "id": self._channel_id(account)},
                              account_id=account.id)
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
                          {"part": "snippet,statistics", "mine": True, "maxResults": 50},
                          account_id=account.id)
        return list(resp.get("items") or [])

    def _uploads_playlist(self, channel_id: str, *, account_id: Optional[str] = None) -> str:
        resp = self._call("channels.list",
                          {"part": "contentDetails", "id": channel_id},
                          account_id=account_id)
        items = resp.get("items") or []
        if not items:
            raise YouTubeAuthError(f"channel not found: {channel_id}")
        return items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    def list_videos(self, account: PlatformAccount, *, limit: int = 50) -> list[Video]:
        channel_id = self._channel_id(account)
        uploads = self._uploads_playlist(channel_id, account_id=account.id)
        video_ids: list[str] = []
        page_token = ""
        while len(video_ids) < limit:
            params = {"part": "snippet,contentDetails", "playlistId": uploads,
                      "maxResults": min(50, limit - len(video_ids))}
            if page_token:
                params["pageToken"] = page_token
            resp = self._call("playlistItems.list", params, account_id=account.id)
            for it in resp.get("items") or []:
                vid = (it.get("contentDetails") or {}).get("videoId")
                if vid:
                    video_ids.append(vid)
            page_token = resp.get("nextPageToken", "")
            if not page_token:
                break
        videos: list[Video] = []
        # Batch videos.list: toi da 50 id / 1 unit - tiet kiem quota nhat.
        # part khong co statistics (day la metadata) nen duoc cache.
        for i in range(0, len(video_ids), 50):
            batch = video_ids[i:i + 50]
            resp = self._call("videos.list",
                              {"part": "snippet,contentDetails,status",
                               "id": ",".join(batch)},
                              account_id=account.id)
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
        # statistics: policy cua _call chan cache -> so lieu luon moi,
        # khong con snapshot cu 300s.
        resp = self._call("videos.list",
                          {"part": "statistics", "id": video.platform_video_id},
                          account_id=account.id)
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
            "filters": f"video=={video.platform_video_id}"}, cache=False,
            account_id=account.id)
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
