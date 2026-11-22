"""approach_01 — Kien truc core da tai khoan va PlatformAdapter thong nhat.

Modular Monolith tien hoa sang Microservices. Core chi lam viec voi
schema chung trong ``src.contracts``; moi nen tang la mot Adapter.
Khong goi truc tiep API nen tang tu core, khong log secret, fail-closed
theo ``ALLOWED_ACCOUNT_TRANSITIONS`` / ``FAIL_CLOSED_STATUSES``.

Chi dung thu vien chuan. Import contracts theo DUY NHAT mot cach:
``from src.contracts import ...`` chay tu repo root. Khong con khoi
fallback hai kieu import nua (xem Fix).

Fix (fix_import_dual_contracts_residual):
- Bo khoi fallback hai kieu import contracts. Kieu du phong co the
  nap cung mot file thanh module thu hai (module tran vs
  ``src.contracts``), lam Enum/dataclass identity (``is`` /
  ``isinstance``) am tham vo giua cac module — pytest khong bat duoc
  vi no luon import qua ``src.*`` tu repo root. Gio chi con mot
  import, mot identity.
- Ghi chu: day la buoc co hoc cho rieng file nay. Cac file khac van
  con fallback (ai, analytics, browser, observability/monitor, risk,
  facebook_connector, scheduler, tiktok_connector) thuoc PR co hoc
  toan src/ tiep theo — khong sua o nhanh nay.
- Khong doi bat ky hanh vi nao khac. Dac biet InMemoryMetricsStore
  .save_snapshot() van tra metric tran nhu tren main (loi kieu tra ve
  VideoStatsSnapshot thuoc fix_core_store_return_type, chua merge,
  khong sua o day).
"""
from __future__ import annotations

import hashlib
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Callable, Optional, Protocol

# DUY NHAT mot cach import contract: tu repo root. Khong fallback.
from src.contracts import (
    ALLOWED_ACCOUNT_TRANSITIONS,
    FAIL_CLOSED_STATUSES,
    MAX_JOB_RETRIES,
    AccountStatus,
    AuthMethod,
    CollectJob,
    JobPriority,
    JobStatus,
    JobType,
    MetricSource,
    Platform,
    PlatformAccount,
    ProxyInfo,
    ProxyStatus,
    TokenSet,
    UnifiedVideoMetric,
    Video,
    VideoStatus,
    is_fail_closed_status,
    utcnow,
    validate_metric,
)

__all__ = [
    "SPLIT_ACCOUNT_THRESHOLD", "SPLIT_VIDEOS_PER_DAY_THRESHOLD",
    "ModuleName", "CoreConfig", "VideoMetricDaily", "Task",
    "PlatformAdapterBase", "AdapterRegistry", "AccountTransitionError",
    "transition_account", "should_split_to_microservices",
    "InMemoryAccountStore", "InMemoryVideoStore", "InMemoryMetricsStore",
    "CoreService",
]

# Nguong tach monolith -> microservices (da khoa trong ARCHITECTURE.md).
SPLIT_ACCOUNT_THRESHOLD = 500
SPLIT_VIDEOS_PER_DAY_THRESHOLD = 10_000


class ModuleName(str, Enum):
    CORE = "core"
    ACCOUNTS = "accounts"
    PROXY = "proxy"
    CONNECTORS = "connectors"
    INGESTION = "ingestion"
    SCHEDULER = "scheduler"
    ANALYTICS = "analytics"
    BROWSER = "browser"
    API = "api"
    WEB = "web"
    OBSERVABILITY = "observability"
    SECURITY = "security"
    RISK = "risk"
    AI = "ai"


@dataclass
class CoreConfig:
    """Cau hinh core; moi gia tri deu co mac dinh an toan (fail-closed)."""

    max_accounts_before_split: int = SPLIT_ACCOUNT_THRESHOLD
    max_videos_per_day_before_split: int = SPLIT_VIDEOS_PER_DAY_THRESHOLD
    default_job_max_retries: int = MAX_JOB_RETRIES
    token_refresh_before_minutes: int = 30
    # 1 account = 1 proxy sticky, 1 profile; khong co gia tri mac dinh chung.
    allow_shared_proxy: bool = False
    allow_shared_profile: bool = False


@dataclass
class VideoMetricDaily:
    """Chi so tong hop 1 video / 1 ngay — rollup cua UnifiedVideoMetric.

    Khong thay the UnifiedVideoMetric (schema chuan duy nhat); day la
    bang tong hop phuc vu dashboard, tinh tu snapshot append-only.
    """

    video_id: str
    account_id: str
    platform: Platform
    date_bucket: str  # YYYY-MM-DD
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    watch_time_seconds: int = 0
    followers_delta: int = 0
    source: MetricSource = MetricSource.PUBLIC_WEB

    @classmethod
    def from_metric(cls, m: UnifiedVideoMetric) -> "VideoMetricDaily":
        return cls(
            video_id=m.video_id, account_id=m.account_id,
            platform=m.platform, date_bucket=m.date_bucket,
            views=m.views, likes=m.likes, comments=m.comments,
            shares=m.shares, saves=m.saves,
            watch_time_seconds=m.watch_time_seconds,
            followers_delta=m.followers_delta, source=m.source,
        )

    @property
    def engagement_rate(self) -> float:
        return (self.likes + self.comments + self.shares) / self.views if self.views > 0 else 0.0


# ``Task`` trong dac ta goc = ``CollectJob`` trong contracts (khong doi ten).
Task = CollectJob


class AccountTransitionError(ValueError):
    """Chuyen trang thai tai khoan khong nam trong ALLOWED_ACCOUNT_TRANSITIONS."""


def transition_account(account: PlatformAccount, to: AccountStatus, *, actor: str = "system") -> PlatformAccount:
    """Chuyen trang thai co kiem tra; tu choi moi chuyen doi bat hop le.

    Tra ve account da cap nhat (mutate + tra ve cung object de tien dung).
    Nguoi goi phai ghi audit log; ham nay khong tu ghi log de tranh
    phu thuoc vong tron vao observability.
    """
    allowed = ALLOWED_ACCOUNT_TRANSITIONS.get(account.status, frozenset())
    if to not in allowed:
        raise AccountTransitionError(
            f"illegal account transition {account.status.value} -> {to.value} "
            f"(account={account.id}, actor={actor})"
        )
    account.status = to
    account.updated_at = utcnow()
    return account


def should_split_to_microservices(*, account_count: int, videos_per_day: int, config: Optional[CoreConfig] = None) -> bool:
    """True khi vuot nguong da khoa: ~500 account hoac >10k video/ngay."""
    cfg = config or CoreConfig()
    return (
        account_count >= cfg.max_accounts_before_split
        or videos_per_day >= cfg.max_videos_per_day_before_split
    )


def new_id() -> str:
    return uuid.uuid4().hex


class PlatformAdapterBase(ABC):
    """Lop co so cho moi adapter; bat buoc tuan thu PlatformAdapter protocol.

    Adapter con chi can cai dat 5 phuong thuc. Lop co so cung cap:
    - ``guard(account)``: fail-closed truoc moi call.
    - ``mask``: khong bao gio de token lo vao exception/log.
    """

    platform: Platform

    def guard(self, account: PlatformAccount) -> None:
        if is_fail_closed_status(account.status):
            raise AdapterAuthError(
                f"account {account.id} is fail-closed ({account.status.value}); "
                "job must be REJECTED, no fallback proxy/session"
            )
        if not account.credential_ref:
            raise AdapterAuthError(f"account {account.id} has no credential_ref")

    @staticmethod
    def mask(value: Optional[str]) -> str:
        if not value:
            return "<none>"
        return f"{value[:3]}***{value[-3:]}" if len(value) > 6 else "***"

    @abstractmethod
    def authenticate(self, account: PlatformAccount) -> TokenSet: ...
    @abstractmethod
    def check_alive(self, account: PlatformAccount) -> bool: ...
    @abstractmethod
    def list_videos(self, account: PlatformAccount, *, limit: int = 50) -> list[Video]: ...
    @abstractmethod
    def get_video_stats(self, account: PlatformAccount, video: Video) -> UnifiedVideoMetric: ...
    @abstractmethod
    def refresh_token(self, account: PlatformAccount, tokens: TokenSet) -> TokenSet: ...


class AdapterAuthError(RuntimeError):
    """Loi xac thuc: pipeline phai chuyen account sang NEEDS_REAUTH, khong retry vo han."""


class AdapterRateLimitError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: Optional[float] = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AdapterRegistry:
    """Registry adapter theo Platform; them nen tang moi = register 1 adapter."""

    def __init__(self) -> None:
        self._adapters: dict[Platform, PlatformAdapterBase] = {}

    def register(self, adapter: PlatformAdapterBase) -> None:
        self._adapters[adapter.platform] = adapter

    def get(self, platform: Platform) -> PlatformAdapterBase:
        try:
            return self._adapters[platform]
        except KeyError as exc:
            raise KeyError(f"no adapter registered for platform={platform.value}") from exc

    def platforms(self) -> list[Platform]:
        return sorted(self._adapters, key=lambda p: p.value)


class InMemoryAccountStore:
    """Store bo nho dung cho test/dev; production thay bang PostgreSQL (source of truth)."""

    def __init__(self) -> None:
        self._by_id: dict[str, PlatformAccount] = {}

    def save(self, account: PlatformAccount) -> PlatformAccount:
        self._by_id[account.id] = account
        return account

    def get(self, account_id: str) -> Optional[PlatformAccount]:
        return self._by_id.get(account_id)

    def list_by_workspace(self, workspace_id: str, *, platform: Optional[Platform] = None,
                          status: Optional[AccountStatus] = None) -> list[PlatformAccount]:
        out = [a for a in self._by_id.values() if a.workspace_id == workspace_id]
        if platform is not None:
            out = [a for a in out if a.platform == platform]
        if status is not None:
            out = [a for a in out if a.status == status]
        return sorted(out, key=lambda a: a.created_at)


class InMemoryVideoStore:
    def __init__(self) -> None:
        self._by_id: dict[str, Video] = {}
        self._by_account: dict[str, list[str]] = {}

    def save(self, video: Video) -> Video:
        self._by_id[video.id] = video
        self._by_account.setdefault(video.account_id, [])
        if video.id not in self._by_account[video.account_id]:
            self._by_account[video.account_id].append(video.id)
        return video

    def get(self, video_id: str) -> Optional[Video]:
        return self._by_id.get(video_id)

    def list_by_account(self, account_id: str) -> list[Video]:
        return [self._by_id[i] for i in self._by_account.get(account_id, [])]


class InMemoryMetricsStore:
    """MetricsStore protocol: snapshot append-only, idempotent theo idempotency_key."""

    def __init__(self) -> None:
        self._by_key: dict[str, UnifiedVideoMetric] = {}
        self._history: dict[str, list[UnifiedVideoMetric]] = {}

    def save_snapshot(self, metric: UnifiedVideoMetric):
        errors = validate_metric(metric)
        if errors:
            raise ValueError(f"invalid metric: {errors}")
        key = metric.idempotency_key
        if key in self._by_key:
            return self._by_key[key]  # idempotent: khong ghi de, khong tao trung
        self._by_key[key] = metric
        self._history.setdefault(metric.video_id, []).append(metric)
        # Tra ve object tuong duong VideoStatsSnapshot (khong import vong tron)
        return metric

    def get_history(self, *, video_id: str, start: datetime, end: datetime) -> list[UnifiedVideoMetric]:
        return [m for m in self._history.get(video_id, []) if start <= m.collected_at <= end]

    def get_latest(self, *, video_id: str) -> Optional[UnifiedVideoMetric]:
        hist = self._history.get(video_id, [])
        return hist[-1] if hist else None

    def daily_rollup(self, *, video_id: str, date_bucket: str) -> Optional[VideoMetricDaily]:
        rows = [m for m in self._history.get(video_id, []) if m.date_bucket == date_bucket]
        if not rows:
            return None
        last = rows[-1]
        return VideoMetricDaily.from_metric(last)


@dataclass
class CoreService:
    """Facade core: account + video + metric + adapter, khong goi API truc tiep.

    Moi thu thap thuc te di qua scheduler/connector; CoreService chi
    dieu phoi entity va kiem tra fail-closed truoc khi giao job.
    """

    config: CoreConfig = field(default_factory=CoreConfig)
    accounts: InMemoryAccountStore = field(default_factory=InMemoryAccountStore)
    videos: InMemoryVideoStore = field(default_factory=InMemoryVideoStore)
    metrics: InMemoryMetricsStore = field(default_factory=InMemoryMetricsStore)
    adapters: AdapterRegistry = field(default_factory=AdapterRegistry)

    def create_account(self, *, workspace_id: str, platform: Platform,
                       platform_user_id: str, display_name: str = "",
                       credential_ref: str, auth_method=None) -> PlatformAccount:
        account = PlatformAccount(
            id=new_id(), workspace_id=workspace_id, platform=platform,
            platform_user_id=platform_user_id, display_name=display_name,
            credential_ref=credential_ref,
            auth_method=auth_method or AuthMethod.OAUTH2,
        )
        return self.accounts.save(account)

    def adapter_for(self, account: PlatformAccount) -> PlatformAdapterBase:
        return self.adapters.get(account.platform)

    def can_collect(self, account: PlatformAccount, proxy: Optional[ProxyInfo]) -> tuple[bool, str]:
        """Gate fail-closed truoc khi tao job: tra (False, ly_do) neu phai REJECTED."""
        if is_fail_closed_status(account.status):
            return False, f"account status {account.status.value} is fail-closed"
        if proxy is None:
            return False, "no proxy bound (fail-closed: no default proxy)"
        if proxy.status != ProxyStatus.ACTIVE:
            return False, f"proxy status {proxy.status.value} is not ACTIVE"
        if account.status not in (AccountStatus.ACTIVE, AccountStatus.VERIFIED, AccountStatus.TOKEN_EXPIRING):
            return False, f"account status {account.status.value} not collectable"
        return True, ""

    def build_collect_job(self, account: PlatformAccount, job_type: JobType,
                          *, priority: JobPriority = JobPriority.NORMAL,
                          video_id: Optional[str] = None) -> CollectJob:
        # Gate proxy that duoc kiem tra o scheduler (co proxy that); o day chi tao job.
        return CollectJob(
            id=new_id(), job_type=job_type, account_id=account.id,
            platform=account.platform, video_id=video_id, priority=priority,
        )
