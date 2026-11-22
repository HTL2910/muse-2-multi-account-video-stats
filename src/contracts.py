"""Hop dong Giao dien & Schema dung chung — multi-account-video-stats.

File nay la contract duy nhat ma moi chuyen gia (approach_01..approach_20)
deu import. Khong duoc doi ten Enum / dataclass / chu ky ham o day neu
chua duoc Team Lead khoa lai. Neu can mo rong, chi THEM truong moi co
gia tri mac dinh hoac THEM gia tri Enum moi — khong doi ten cu.

Quy uoc:
- Moi timestamp deu la ``datetime`` timezone-aware UTC.
- Moi id noi bo deu la ``str`` (UUID hex). Id goc cua nen tang nam o
  ``platform_*_id`` va khong bao gio dung lam khoa chinh noi bo.
- Khong bao gio log / tra ve secret dang plaintext (xem ``SecretVault``).
- Fail-closed: proxy chet, session het han, token invalid -> job bi
  REJECTED, khong dung proxy/session mac dinh cua account khac.

Python 3.11+, chi dung thu vien chuan (dataclasses, enum, typing).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Generic, Optional, Protocol, Sequence, TypeVar

__all__ = [
    "CONTRACTS_VERSION",
    "MAX_JOB_RETRIES",
    "RISK_REDUCE_THRESHOLD",
    "RISK_STOP_THRESHOLD",
    "TOKEN_REFRESH_BEFORE_MINUTES",
    "PROXY_QUARANTINE_FAILURES",
    "Platform",
    "AccountStatus",
    "AuthMethod",
    "ProxyType",
    "ProxyKind",
    "ProxyStatus",
    "JobType",
    "JobStatus",
    "JobPriority",
    "MetricSource",
    "VideoStatus",
    "DataQuality",
    "BatchDecision",
    "RiskLevel",
    "Role",
    "AlertSeverity",
    "AlertType",
    "Workspace",
    "PlatformAccount",
    "TokenSet",
    "ProxyInfo",
    "ProxyBinding",
    "ProxyHealth",
    "BrowserProfile",
    "Video",
    "UnifiedVideoMetric",
    "VideoStatsSnapshot",
    "CollectJob",
    "JobResult",
    "SyncRequest",
    "SyncResult",
    "AccountHealth",
    "RiskAssessment",
    "AccountGroup",
    "Alert",
    "AuditLogEntry",
    "PaginatedResult",
    "ViewForecast",
    "Recommendation",
    "GoldenCase",
    "PlatformAdapter",
    "SecretVault",
    "ProxyPool",
    "MetricsStore",
    "TaskScheduler",
    "BrowserProfileManager",
    "AccountHealthScorer",
    "AlertNotifier",
    "ALLOWED_ACCOUNT_TRANSITIONS",
    "FAIL_CLOSED_STATUSES",
    "utcnow",
    "date_bucket",
    "build_idempotency_key",
    "compute_engagement_rate",
    "compute_growth_rate",
    "validate_metric",
    "is_fail_closed_status",
    "is_terminal_job_status",
]

CONTRACTS_VERSION = "1.0.0"

# --- Hang so van hanh dung chung -------------------------------------------
MAX_JOB_RETRIES = 3
RISK_REDUCE_THRESHOLD = 0.7
RISK_STOP_THRESHOLD = 0.9
TOKEN_REFRESH_BEFORE_MINUTES = 30
PROXY_QUARANTINE_FAILURES = 3


# ==========================================================================
# Enums
# ==========================================================================


class Platform(str, Enum):
    FACEBOOK = "facebook"
    TIKTOK = "tiktok"
    YOUTUBE = "youtube"


class AccountStatus(str, Enum):
    CREATED = "created"
    VERIFIED = "verified"
    ACTIVE = "active"
    TOKEN_EXPIRING = "token_expiring"
    REFRESHING = "refreshing"
    NEEDS_REVIEW = "needs_review"
    NEEDS_REAUTH = "needs_reauth"
    CHECKPOINT = "checkpoint"
    CAPTCHA_REQUIRED = "captcha_required"
    TOKEN_EXPIRED = "token_expired"
    RATE_LIMITED = "rate_limited"
    BANNED = "banned"
    DISABLED = "disabled"
    SUSPENDED = "suspended"


class AuthMethod(str, Enum):
    OAUTH2 = "oauth2"
    COOKIE = "cookie"
    PASSWORD = "password"
    PASSWORD_TOTP = "password_totp"


class ProxyType(str, Enum):
    HTTP = "http"
    HTTPS = "https"
    SOCKS5 = "socks5"


class ProxyKind(str, Enum):
    RESIDENTIAL = "residential"
    DATACENTER = "datacenter"
    MOBILE = "mobile"


class ProxyStatus(str, Enum):
    ACTIVE = "active"
    WARMUP = "warmup"
    QUARANTINED = "quarantined"
    DEAD = "dead"


class JobType(str, Enum):
    LOGIN = "login"
    CHECK_ALIVE = "check_alive"
    SYNC_VIDEOS = "sync_videos"
    FETCH_STATS = "fetch_stats"
    REFRESH_TOKEN = "refresh_token"


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RETRYING = "retrying"
    DEAD_LETTER = "dead_letter"
    REJECTED = "rejected"


class JobPriority(int, Enum):
    LOW = 0
    NORMAL = 1
    HIGH = 2
    VIP = 3


class MetricSource(str, Enum):
    FACEBOOK_GRAPH_API = "facebook_graph_api"
    TIKTOK_API = "tiktok_api"
    YOUTUBE_DATA_API = "youtube_data_api"
    YOUTUBE_ANALYTICS_API = "youtube_analytics_api"
    BROWSER_FALLBACK = "browser_fallback"
    PUBLIC_WEB = "public_web"


class VideoStatus(str, Enum):
    ACTIVE = "active"
    PRIVATE = "private"
    DELETED = "deleted"
    HIDDEN = "hidden"


class DataQuality(str, Enum):
    OK = "ok"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"


class BatchDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"
    PARTIAL = "partial"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Role(str, Enum):
    ADMIN = "admin"
    MANAGER = "manager"
    OPERATOR = "operator"
    VIEWER = "viewer"


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertType(str, Enum):
    ACCOUNT_CHECKPOINT = "account_checkpoint"
    ACCOUNT_BANNED = "account_banned"
    TOKEN_EXPIRING = "token_expiring"
    PROXY_DEAD = "proxy_dead"
    SYNC_FAILED = "sync_failed"
    VIDEO_SPIKE = "video_spike"
    VIDEO_DROP = "video_drop"
    VIDEO_REMOVED = "video_removed"
    QUOTA_WARNING = "quota_warning"


# --- May trang thai tai khoan ----------------------------------------------
# Chi cac chuyen doi trong bang nay moi hop le. Moi chuyen doi khac phai bi
# tu choi va ghi audit log. Xem approach_02 / approach_14.
ALLOWED_ACCOUNT_TRANSITIONS: dict[AccountStatus, frozenset[AccountStatus]] = {
    AccountStatus.CREATED: frozenset(
        {AccountStatus.VERIFIED, AccountStatus.DISABLED}
    ),
    AccountStatus.VERIFIED: frozenset(
        {AccountStatus.ACTIVE, AccountStatus.NEEDS_REVIEW, AccountStatus.DISABLED}
    ),
    AccountStatus.ACTIVE: frozenset(
        {
            AccountStatus.TOKEN_EXPIRING,
            AccountStatus.NEEDS_REVIEW,
            AccountStatus.NEEDS_REAUTH,
            AccountStatus.CHECKPOINT,
            AccountStatus.CAPTCHA_REQUIRED,
            AccountStatus.TOKEN_EXPIRED,
            AccountStatus.RATE_LIMITED,
            AccountStatus.BANNED,
            AccountStatus.DISABLED,
            AccountStatus.SUSPENDED,
        }
    ),
    AccountStatus.TOKEN_EXPIRING: frozenset(
        {
            AccountStatus.REFRESHING,
            AccountStatus.TOKEN_EXPIRED,
            AccountStatus.NEEDS_REAUTH,
            AccountStatus.ACTIVE,
        }
    ),
    AccountStatus.REFRESHING: frozenset(
        {
            AccountStatus.ACTIVE,
            AccountStatus.TOKEN_EXPIRED,
            AccountStatus.NEEDS_REAUTH,
            AccountStatus.NEEDS_REVIEW,
        }
    ),
    AccountStatus.NEEDS_REVIEW: frozenset(
        {AccountStatus.ACTIVE, AccountStatus.DISABLED, AccountStatus.BANNED}
    ),
    AccountStatus.NEEDS_REAUTH: frozenset(
        {AccountStatus.ACTIVE, AccountStatus.NEEDS_REVIEW, AccountStatus.DISABLED}
    ),
    AccountStatus.CHECKPOINT: frozenset(
        {
            AccountStatus.NEEDS_REVIEW,
            AccountStatus.ACTIVE,
            AccountStatus.BANNED,
            AccountStatus.DISABLED,
        }
    ),
    AccountStatus.CAPTCHA_REQUIRED: frozenset(
        {
            AccountStatus.NEEDS_REVIEW,
            AccountStatus.ACTIVE,
            AccountStatus.CHECKPOINT,
            AccountStatus.DISABLED,
        }
    ),
    AccountStatus.TOKEN_EXPIRED: frozenset(
        {AccountStatus.NEEDS_REAUTH, AccountStatus.ACTIVE, AccountStatus.DISABLED}
    ),
    AccountStatus.RATE_LIMITED: frozenset(
        {AccountStatus.ACTIVE, AccountStatus.NEEDS_REVIEW}
    ),
    AccountStatus.BANNED: frozenset({AccountStatus.DISABLED}),
    AccountStatus.DISABLED: frozenset({AccountStatus.CREATED}),
    AccountStatus.SUSPENDED: frozenset(
        {AccountStatus.NEEDS_REVIEW, AccountStatus.DISABLED, AccountStatus.BANNED}
    ),
}

# Trang thai fail-closed: job tu dong cua account phai dung ngay.
FAIL_CLOSED_STATUSES: frozenset[AccountStatus] = frozenset(
    {
        AccountStatus.CHECKPOINT,
        AccountStatus.CAPTCHA_REQUIRED,
        AccountStatus.TOKEN_EXPIRED,
        AccountStatus.NEEDS_REAUTH,
        AccountStatus.BANNED,
        AccountStatus.DISABLED,
        AccountStatus.SUSPENDED,
    }
)


# ==========================================================================
# Dataclasses — Account / Workspace / Proxy / Browser
# ==========================================================================


@dataclass
class Workspace:
    """Tenant / workspace: moi tenant chi thay account cua minh."""

    id: str
    name: str
    owner_id: str
    created_at: datetime = field(default_factory=lambda: utcnow())


@dataclass
class TokenSet:
    """Bo token OAuth. KHONG bao gio log gia tri that — chi log mask/expiry.

    ``access_token`` / ``refresh_token`` o day chi ton tai trong bo nho
    worker sau khi giai ma tu Vault, khong luu vao DB/log.
    """

    access_token: Optional[str] = None
    refresh_token: Optional[str] = None
    token_type: str = "Bearer"
    expires_at: Optional[datetime] = None
    scopes: list[str] = field(default_factory=list)

    def is_expired(self, *, at: Optional[datetime] = None) -> bool:
        if self.expires_at is None:
            return False
        return (at or utcnow()) >= self.expires_at

    def needs_refresh(
        self, *, before_minutes: int = TOKEN_REFRESH_BEFORE_MINUTES
    ) -> bool:
        if self.expires_at is None:
            return False
        from datetime import timedelta

        return utcnow() + timedelta(minutes=before_minutes) >= self.expires_at

    def masked(self) -> dict[str, Any]:
        return {
            "has_access_token": self.access_token is not None,
            "has_refresh_token": self.refresh_token is not None,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "scopes": list(self.scopes),
        }


@dataclass
class PlatformAccount:
    """Entity tai khoan thong nhat cho Facebook / TikTok / YouTube."""

    id: str  # UUID noi bo
    workspace_id: str
    platform: Platform
    platform_user_id: str  # id goc cua nen tang
    display_name: str = ""
    avatar_url: str = ""
    status: AccountStatus = AccountStatus.CREATED
    auth_method: AuthMethod = AuthMethod.OAUTH2
    credential_ref: str = ""  # tham chieu vao Vault, khong phai secret
    proxy_binding_id: Optional[str] = None  # 1 account = 1 proxy sticky
    profile_id: Optional[str] = None  # 1 account = 1 browser profile
    owner: str = ""
    niche: str = ""
    tags: list[str] = field(default_factory=list)
    group_ids: list[str] = field(default_factory=list)
    health_score: float = 1.0  # 0.0 .. 1.0
    risk_level: RiskLevel = RiskLevel.LOW
    last_check_at: Optional[datetime] = None
    last_sync_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: utcnow())
    updated_at: datetime = field(default_factory=lambda: utcnow())


@dataclass
class ProxyInfo:
    """Mot proxy trong pool. Credential proxy duoc ma hoa o Vault."""

    id: str
    host: str
    port: int
    proxy_type: ProxyType = ProxyType.HTTP
    kind: ProxyKind = ProxyKind.RESIDENTIAL
    country: str = ""
    isp: str = ""
    username: Optional[str] = None
    # Mat khau proxy KHONG luu o day; luu o Vault theo credential_ref.
    credential_ref: str = ""
    status: ProxyStatus = ProxyStatus.ACTIVE
    score: float = 0.5  # 0.0 .. 1.0, cap nhat EWMA sau moi request
    success_rate: float = 1.0
    avg_latency_ms: float = 0.0
    consecutive_failures: int = 0
    last_check_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=lambda: utcnow())

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class ProxyBinding:
    """Lich su gan sticky account <-> proxy (de truy vet khi bi khoa)."""

    id: str
    account_id: str
    proxy_id: str
    bound_at: datetime = field(default_factory=lambda: utcnow())
    unbound_at: Optional[datetime] = None
    reason: str = ""


@dataclass
class ProxyHealth:
    """Ket qua mot lan health-check proxy."""

    proxy_id: str
    checked_at: datetime = field(default_factory=lambda: utcnow())
    ok: bool = True
    latency_ms: float = 0.0
    success_rate: float = 1.0
    ip_leak: bool = False
    dns_leak: bool = False
    blacklisted: bool = False
    error: str = ""


@dataclass
class BrowserProfile:
    """Browser profile co lap cho 1 account (Playwright persistent context)."""

    id: str
    account_id: str
    user_data_dir: str
    user_agent: str = ""
    timezone: str = "UTC"
    locale: str = "en-US"
    viewport_width: int = 1280
    viewport_height: int = 720
    proxy_id: Optional[str] = None  # phai trung proxy sticky cua account
    fingerprint_key: str = ""  # hash on dinh cua fingerprint, khong phai du lieu tho
    created_at: datetime = field(default_factory=lambda: utcnow())
    last_used_at: Optional[datetime] = None


# ==========================================================================
# Dataclasses — Video & Metrics (schema chuan duy nhat)
# ==========================================================================


@dataclass
class Video:
    """Video chuan noi bo, map tu video goc cua tung nen tang."""

    id: str  # UUID noi bo
    account_id: str
    platform: Platform
    platform_video_id: str
    title: str = ""
    description: str = ""
    thumbnail_url: str = ""
    url: str = ""
    duration_seconds: Optional[int] = None
    published_at: Optional[datetime] = None
    status: VideoStatus = VideoStatus.ACTIVE
    created_at: datetime = field(default_factory=lambda: utcnow())
    updated_at: datetime = field(default_factory=lambda: utcnow())


@dataclass(frozen=True)
class UnifiedVideoMetric:
    """Schema chi so chuan DUY NHAT. Moi adapter phai map ve schema nay.

    Don vi: views/likes/comments/shares/saves la so luong; watch_time la
    giay; followers_delta la chenh lech follower so voi snapshot truoc.
    ``collected_at`` la thoi diem thu thap (UTC), ``date_bucket`` la ngay
    YYYY-MM-DD dung lam khoa idempotency.
    """

    video_id: str  # UUID noi bo (Video.id)
    account_id: str
    platform: Platform
    platform_video_id: str
    views: int = 0
    likes: int = 0
    comments: int = 0
    shares: int = 0
    saves: int = 0
    watch_time_seconds: int = 0
    followers_delta: int = 0
    impressions: Optional[int] = None
    ctr: Optional[float] = None  # 0.0 .. 1.0
    collected_at: datetime = field(default_factory=lambda: utcnow())
    source: MetricSource = MetricSource.PUBLIC_WEB
    quality: DataQuality = DataQuality.OK
    date_bucket: str = ""  # YYYY-MM-DD; neu rong thi lay tu collected_at

    def __post_init__(self) -> None:
        if not self.date_bucket:
            object.__setattr__(
                self, "date_bucket", self.collected_at.date().isoformat()
            )

    @property
    def idempotency_key(self) -> str:
        return build_idempotency_key(
            self.account_id, self.platform_video_id, self.date_bucket
        )

    @property
    def engagement_rate(self) -> float:
        return compute_engagement_rate(
            self.views, self.likes, self.comments, self.shares
        )


@dataclass
class VideoStatsSnapshot:
    """Snapshot chi so theo thoi gian (luu append-only, khong ghi de)."""

    id: str
    metric: UnifiedVideoMetric
    raw_payload_ref: str = ""  # tham chieu JSONB tho (co TTL), de truy vet
    created_at: datetime = field(default_factory=lambda: utcnow())


# ==========================================================================
# Dataclasses — Job / Sync / Health / Risk / Alert / Audit
# ==========================================================================


@dataclass
class CollectJob:
    """Mot job thu thap trong hang doi uu tien."""

    id: str
    job_type: JobType
    account_id: str
    platform: Platform
    video_id: Optional[str] = None  # chi dung cho FETCH_STATS
    priority: JobPriority = JobPriority.NORMAL
    idempotency_key: str = ""
    status: JobStatus = JobStatus.PENDING
    attempts: int = 0
    max_retries: int = MAX_JOB_RETRIES
    scheduled_at: datetime = field(default_factory=lambda: utcnow())
    lease_until: Optional[datetime] = None
    payload: dict[str, Any] = field(default_factory=dict)
    last_error: str = ""

    def __post_init__(self) -> None:
        if not self.idempotency_key:
            seed = f"{self.job_type.value}:{self.account_id}:{self.video_id or ''}"
            self.idempotency_key = hashlib.sha256(seed.encode()).hexdigest()[:32]

    @property
    def can_retry(self) -> bool:
        return self.attempts < self.max_retries


@dataclass
class JobResult:
    """Ket qua tra ve cua mot job (thanh cong, that bai, hoac bi reject)."""

    job_id: str
    status: JobStatus
    account_id: str
    videos_found: int = 0
    metrics_written: int = 0
    error: str = ""
    error_code: str = ""  # vd: RATE_LIMITED, CHECKPOINT, PROXY_DEAD
    decision: BatchDecision = BatchDecision.ACCEPT
    finished_at: datetime = field(default_factory=lambda: utcnow())


@dataclass
class SyncRequest:
    """Yeu cau dong bo tu API/Dashboard."""

    account_id: str
    job_type: JobType = JobType.SYNC_VIDEOS
    priority: JobPriority = JobPriority.NORMAL
    force: bool = False
    requested_by: str = ""


@dataclass
class SyncResult:
    """Ket qua dong bo tra ve cho API/Dashboard (khong chua secret)."""

    account_id: str
    job_id: str
    status: JobStatus
    videos_found: int = 0
    metrics_written: int = 0
    decision: BatchDecision = BatchDecision.ACCEPT
    message: str = ""


@dataclass
class AccountHealth:
    """Suc khoe tai khoan tong hop (de dashboard + scheduler dung)."""

    account_id: str
    health_score: float = 1.0  # 0.0 .. 1.0
    risk_level: RiskLevel = RiskLevel.LOW
    sync_success_rate: float = 1.0
    checkpoint_count_30d: int = 0
    captcha_count_30d: int = 0
    rate_limit_count_30d: int = 0
    proxy_failure_count_30d: int = 0
    computed_at: datetime = field(default_factory=lambda: utcnow())


@dataclass
class RiskAssessment:
    """Ket qua du doan rui ro khoa/checkpoint cho 1 account."""

    account_id: str
    risk_score: float = 0.0  # 0.0 .. 1.0
    risk_level: RiskLevel = RiskLevel.LOW
    top_features: list[str] = field(default_factory=list)
    action: str = "none"  # none | reduce_frequency | change_proxy | stop
    assessed_at: datetime = field(default_factory=lambda: utcnow())


@dataclass
class AccountGroup:
    """Nhom account theo chien dich / kenh / chu so huu."""

    id: str
    workspace_id: str
    name: str
    account_ids: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)


@dataclass
class Alert:
    """Canh bao van hanh (hien thi trong app + day Telegram/Webhook)."""

    id: str
    workspace_id: str
    alert_type: AlertType
    severity: AlertSeverity = AlertSeverity.WARNING
    account_id: Optional[str] = None
    video_id: Optional[str] = None
    title: str = ""
    message: str = ""
    is_read: bool = False
    created_at: datetime = field(default_factory=lambda: utcnow())


@dataclass
class AuditLogEntry:
    """Ban ghi audit append-only, co hash-chain (khong sua/xoa duoc)."""

    id: str
    workspace_id: str
    actor_id: str
    action: str  # vd: account.create, proxy.assign, account.delete
    target_type: str  # account | proxy | video | credential | group
    target_id: str
    detail: dict[str, Any] = field(default_factory=dict)
    prev_hash: str = ""
    entry_hash: str = ""
    created_at: datetime = field(default_factory=lambda: utcnow())


T = TypeVar("T")


@dataclass
class PaginatedResult(Generic[T]):
    """Ket qua phan trang cursor-based dung chung cho API."""

    items: list[T] = field(default_factory=list)
    next_cursor: Optional[str] = None
    total: Optional[int] = None


@dataclass
class ViewForecast:
    """Du bao views cho 1 video (chi la goi y, kem khoang tin cay)."""

    video_id: str
    horizon: str = "24h"  # 24h | 7d
    predicted_views: float = 0.0
    lower_bound: float = 0.0
    upper_bound: float = 0.0
    confidence: float = 0.0  # 0.0 .. 1.0
    model: str = ""
    forecasted_at: datetime = field(default_factory=lambda: utcnow())


@dataclass
class Recommendation:
    """Goi y noi dung / thoi diem dang (khong tu dong dang)."""

    account_id: str
    title: str = ""
    reason: str = ""
    confidence: float = 0.0
    video_ids: list[str] = field(default_factory=list)
    best_posting_hours: list[int] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: utcnow())


@dataclass(frozen=True)
class GoldenCase:
    """Case co dinh cho CI: phat hien ngay khi API nen tang doi schema."""

    id: str
    platform: Platform
    name: str
    raw_fixture_path: str  # vd: data/golden/youtube/video_list.json
    expected_video_count: int = 0
    expected_metric: Optional[UnifiedVideoMetric] = None


# ==========================================================================
# Protocols — Interface chinh (tat ca adapter/module phai tuan theo)
# ==========================================================================


class PlatformAdapter(Protocol):
    """Interface chung cho Facebook / TikTok / YouTube adapter.

    Moi phuong thuc nhan ``account`` (da co credential_ref + proxy) va
    KHONG tu y doi proxy/session cua account khac. Loi xac thuc phai tra
    ve ``JobResult`` / raise ``AdapterAuthError`` tuyen ro, de pipeline
    chuyen account sang NEEDS_REAUTH thay vi retry vo han.
    """

    platform: Platform

    def authenticate(self, account: PlatformAccount) -> TokenSet:
        """Xac thuc / lay token moi cho account. Tra ve TokenSet."""
        ...

    def check_alive(self, account: PlatformAccount) -> bool:
        """Kiem tra account con song khong (True = song)."""
        ...

    def list_videos(
        self, account: PlatformAccount, *, limit: int = 50
    ) -> list[Video]:
        """Liet ke video cua account (da map ve schema Video chuan)."""
        ...

    def get_video_stats(
        self, account: PlatformAccount, video: Video
    ) -> UnifiedVideoMetric:
        """Lay chi so 1 video, tra ve UnifiedVideoMetric chuan."""
        ...

    def refresh_token(
        self, account: PlatformAccount, tokens: TokenSet
    ) -> TokenSet:
        """Refresh token truoc khi het han. Tra ve TokenSet moi."""
        ...


class SecretVault(Protocol):
    """Vault ma hoa credential/cookie/token (envelope AES-256-GCM)."""

    def store(
        self, *, account_id: str, kind: str, secret: str
    ) -> str:
        """Luu secret, tra ve credential_ref (khong phai secret)."""
        ...

    def retrieve(self, *, credential_ref: str) -> str:
        """Giai ma va tra ve secret trong bo nho worker. Chi goi tai worker."""
        ...

    def rotate(self, *, credential_ref: str, new_secret: str) -> str:
        """Xoay secret, tra ve credential_ref moi (versioned)."""
        ...

    def delete(self, *, credential_ref: str) -> None:
        """Xoa secret vinh vien."""
        ...


class ProxyPool(Protocol):
    """Pool proxy: cap phat sticky, health-check, failover."""

    def acquire(self, account_id: str) -> ProxyInfo:
        """Lay proxy sticky cua account (fail-closed neu khong co proxy tot)."""
        ...

    def release(self, account_id: str) -> None:
        """Giai phong lease proxy cua account."""
        ...

    def report_result(
        self, *, proxy_id: str, ok: bool, latency_ms: float
    ) -> None:
        """Cap nhat score EWMA sau moi request."""
        ...

    def health_check(self, proxy_id: str) -> ProxyHealth:
        """Kiem tra suc khoe 1 proxy."""
        ...

    def failover(self, account_id: str) -> ProxyInfo:
        """Chuyen account sang proxy du phong cung geo khi proxy chinh chet."""
        ...


class MetricsStore(Protocol):
    """Kho luu snapshot metrics (PostgreSQL + TimescaleDB/ClickHouse)."""

    def save_snapshot(self, metric: UnifiedVideoMetric) -> VideoStatsSnapshot:
        """Luu snapshot append-only, idempotent theo idempotency_key."""
        ...

    def get_history(
        self,
        *,
        video_id: str,
        start: datetime,
        end: datetime,
    ) -> list[UnifiedVideoMetric]:
        """Lay lich su metrics cua 1 video theo khoang thoi gian."""
        ...

    def get_latest(self, *, video_id: str) -> Optional[UnifiedVideoMetric]:
        """Lay snapshot moi nhat cua 1 video."""
        ...


class TaskScheduler(Protocol):
    """Scheduler / hang doi tac vu phan tan."""

    def enqueue(self, job: CollectJob) -> CollectJob:
        """Dua job vao hang doi uu tien, tra ve job da co id/status."""
        ...

    def requeue_dead_letter(self, job_id: str) -> CollectJob:
        """Dua job tu dead-letter queue tro lai hang doi."""
        ...


class BrowserProfileManager(Protocol):
    """Quan ly browser profile co lap theo account."""

    def get_or_create(self, account: PlatformAccount) -> BrowserProfile:
        """Lay hoac tao profile rieng cho account (1-1, khong dung chung)."""
        ...

    def open_for_manual_action(
        self, account: PlatformAccount
    ) -> str:
        """Mo profile headful (noVNC/streaming URL) de xu ly captcha."""
        ...

    def close(self, profile_id: str) -> None:
        """Dong profile va don sach tien trinh tam."""
        ...


class AccountHealthScorer(Protocol):
    """Cham diem suc khoe + du doan rui ro khoa/checkpoint."""

    def score(self, account_id: str) -> AccountHealth:
        """Tinh health score hien tai cua account."""
        ...

    def assess_risk(self, account_id: str) -> RiskAssessment:
        """Du doan risk_score 0-1 va hanh dong fail-closed tuong ung."""
        ...


class AlertNotifier(Protocol):
    """Day canh bao ra notification center + Telegram/Webhook/Discord."""

    def notify(self, alert: Alert) -> None:
        """Gui 1 canh bao theo cau hinh cua workspace."""
        ...


# ==========================================================================
# Helpers dung chung (khong phu thuoc thu vien ngoai)
# ==========================================================================


def utcnow() -> datetime:
    """Thoi diem hien tai, timezone-aware UTC."""
    return datetime.now(timezone.utc)


def date_bucket(moment: datetime) -> str:
    """Ngay YYYY-MM-DD (UTC) dung lam khoa idempotency theo ngay."""
    return moment.astimezone(timezone.utc).date().isoformat()


def build_idempotency_key(
    account_id: str, platform_video_id: str, bucket: str | date | datetime
) -> str:
    """Khoa idempotency duy nhat cho (account, video, ngay).

    Moi pipeline/store deu phai dung ham nay — khong tu build key rieng.
    """
    if isinstance(bucket, datetime):
        bucket_str = date_bucket(bucket)
    elif isinstance(bucket, date):
        bucket_str = bucket.isoformat()
    else:
        bucket_str = bucket
    raw = f"{account_id}:{platform_video_id}:{bucket_str}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_engagement_rate(
    views: int, likes: int, comments: int, shares: int
) -> float:
    """Engagement rate = (likes + comments + shares) / views. 0 neu views <= 0."""
    if views <= 0:
        return 0.0
    return (likes + comments + shares) / views


def compute_growth_rate(old_value: float, new_value: float) -> float:
    """Tang truong tuong doi. Tra ve 0.0 neu old_value <= 0 (tranh chia 0)."""
    if old_value <= 0:
        return 0.0 if new_value <= 0 else math.inf
    return (new_value - old_value) / old_value


def validate_metric(metric: UnifiedVideoMetric) -> list[str]:
    """Kiem tra P0 cho 1 metric. Tra ve danh sach loi (rong = hop le).

    Loi P0: gia tri am, thieu truong bat buoc, ctr ngoai [0,1].
    Metric loi phai bi danh dau NEEDS_REVIEW, khong dien 0 hoac suy doan.
    """
    errors: list[str] = []
    if not metric.video_id:
        errors.append("missing video_id")
    if not metric.account_id:
        errors.append("missing account_id")
    if not metric.platform_video_id:
        errors.append("missing platform_video_id")
    for name in ("views", "likes", "comments", "shares", "saves"):
        if getattr(metric, name) < 0:
            errors.append(f"negative {name}")
    if metric.watch_time_seconds < 0:
        errors.append("negative watch_time_seconds")
    if metric.impressions is not None and metric.impressions < 0:
        errors.append("negative impressions")
    if metric.ctr is not None and not 0.0 <= metric.ctr <= 1.0:
        errors.append("ctr out of range [0, 1]")
    return errors


def is_fail_closed_status(status: AccountStatus) -> bool:
    """True neu account o trang thai phai dung moi job tu dong ngay."""
    return status in FAIL_CLOSED_STATUSES


def is_terminal_job_status(status: JobStatus) -> bool:
    """True neu job da ket thuc (khong retry them)."""
    return status in {
        JobStatus.SUCCEEDED,
        JobStatus.DEAD_LETTER,
        JobStatus.REJECTED,
    }
