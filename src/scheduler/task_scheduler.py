"""approach_09 — Scheduler va hang doi tac vu phan tan duoi rang buoc quota.

Chi dung thu vien chuan; Redis/Celery la tang transport co the thay the
(``RedisBackend`` chi duoc nap khi co redis). Ban in-memory nay dung cho
test/dev va la fallback khi Redis chet — job khong mat vi van nam trong
hang doi uu tien + dead-letter, nhung khong ben vung qua restart (ghi ro).

Rang buoc bat buoc:
- per-account concurrency = 1 (distributed lock theo account_id)
- per-proxy concurrency gioi han, per-platform quota/ngay = token bucket
- shard worker theo account_id (consistent hashing) — cung account luon
  ve cung worker, tranh 2 worker cung dang nhap 1 account
- job co lease/visibility timeout + heartbeat, retry exponential backoff
  toi da 3 lan (MAX_JOB_RETRIES) roi vao dead-letter queue
- uu tien theo do tuoi du lieu, deadline va chi phi quota
  (weighted fair scheduling), jitter ngau nhien chong dong loat.
Fail-closed: account/proxy o trang thai fail-closed -> job REJECTED,
khong enqueue lai, khong dung proxy mac dinh cua account khac.
Fix ``fix_scheduler_enqueue_fail_open``: trang thai KHONG BIET khong
con duoc coi la ACTIVE. Account chua dang ky/chua dong bo trang thai,
hoac proxy da bind nhung chua ro trang thai, deu bi REJECTED o enqueue
(va ca o requeue_dead_letter). Caller phai dang ky trang thai that qua
``register_account`` / ``register_proxy`` / ``sync_statuses`` /
``sync_from_store`` hoac truyen trang thai tuong minh vao ``enqueue``.
Fix ``fix_scheduler_quota_proxy_per_account``: quota chi tru MOT LAN,
o MOT diem duy nhat ``QuotaManager.try_acquire()``. Truoc day
``acquire_job`` goi ``acquire_proxy_slot()`` (tang _proxy_running)
roi goi ``allow()`` (lai kiem tra _proxy_running >= limit) nen gioi
han thuc luon bang cau hinh tru 1 (limit=1 thi khong job nao chay);
``_account_buckets`` duoc tao nhung ``allow()`` khong bao gio dung.
Tu ban nay: ``allow()`` chi la peek khong co tac dung phu (khong tru
token, khong tang proxy); ``try_acquire()`` kiem tra ca 3 lop
(account / proxy / platform) truoc, roi moi tru token + tang proxy
nguyen tu duoi lock, that bai thi khong tru gi ca. Lop per-account
la token bucket that, moi account 1 bucket rieng theo
``per_account_quota_per_day``.
Fix ``fix_scheduler_scheduled_at_lost`` (khoi phuc fix cu bi mat khi
merge): truoc day ``PriorityQueue.pop()`` / ``acquire_job()`` lay job
bat ke ``scheduled_at`` nen job FETCH_STATS hen +6h / +15 phut va job
retry sau backoff deu bi chay ngay lap tuc. Tu ban nay queue chi tra
job DA TOI HAN (``scheduled_at <= now``): ``PriorityQueue`` nhan clock
inject duoc, ``pop_due(now)`` quet TOAN heap tim job due tot nhat theo
sort key (khong chi peek head — job VIP tuong lai o dau heap khong
duoc chan job thuong da due phia sau), ``pop()`` bat buoc due,
``pop_any()`` chi la escape hatch drain/test; ``acquire_job()`` dung
``pop_due`` voi now snapshot mot lan moi lan goi; ``finish_job()``
giu nguyen cach tinh ``scheduled_at`` retry (nay moi thuc su duoc
ton trong). Fix quota / enqueue gate giu nguyen, khong thay doi.
"""
from __future__ import annotations

import hashlib
import heapq
import math
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

try:
    from src.contracts import (  # type: ignore
        MAX_JOB_RETRIES, AccountStatus, CollectJob, JobPriority, JobStatus,
        JobType, Platform, PlatformAccount, ProxyInfo, ProxyStatus,
        is_fail_closed_status, utcnow,
    )
except ImportError:
    from ..contracts import (  # type: ignore
        MAX_JOB_RETRIES, AccountStatus, CollectJob, JobPriority, JobStatus,
        JobType, Platform, PlatformAccount, ProxyInfo, ProxyStatus,
        is_fail_closed_status, utcnow,
    )

__all__ = [
    "TokenBucket", "LeakyBucket", "ConsistentHashRing", "DistributedLock",
    "PriorityQueue", "DeadLetterQueue", "QuotaManager", "TaskSchedulerImpl",
    "schedule_jobs_for_account", "NEW_VIDEO_INTERVAL", "OLD_VIDEO_INTERVAL",
    "LEASE_SECONDS", "HEARTBEAT_SECONDS",
]

NEW_VIDEO_INTERVAL = timedelta(minutes=15)
OLD_VIDEO_INTERVAL = timedelta(hours=6)   # video cu: 6-12h, mac dinh 6h
LEASE_SECONDS = 300
HEARTBEAT_SECONDS = 60

# Proxy chi dung duoc o hai trang thai nay; QUARANTINED/DEAD -> REJECTED.
_USABLE_PROXY_STATUSES = frozenset({ProxyStatus.ACTIVE, ProxyStatus.WARMUP})


class TokenBucket:
    """Token bucket: ``rate`` token/giay, toi da ``capacity`` token."""

    def __init__(self, *, rate: float, capacity: float, clock: Callable[[], float] = time.monotonic):
        self.rate, self.capacity, self._clock = rate, capacity, clock
        self._tokens = capacity
        self._last = clock()

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def try_consume(self, n: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False

    def wait_seconds(self, n: float = 1.0) -> float:
        self._refill()
        return 0.0 if self._tokens >= n else (n - self._tokens) / self.rate


class LeakyBucket:
    """Leaky bucket: queue toi da ``capacity``, ro ``rate`` job/giay (lam min burst)."""

    def __init__(self, *, rate: float, capacity: int):
        self.rate, self.capacity = rate, capacity
        self._queue: list[float] = []

    def try_enqueue(self, *, now: Optional[float] = None) -> bool:
        t = time.monotonic() if now is None else now
        self._queue = [x for x in self._queue if x > t - 1.0 / self.rate * self.capacity]
        if len(self._queue) >= self.capacity:
            return False
        self._queue.append(t)
        return True


class ConsistentHashRing:
    """Consistent hashing: account_id -> worker. Them/bot worker chi doi
    ~1/N key, cung account luon ve cung worker."""

    def __init__(self, workers: list[str], *, replicas: int = 100):
        self.replicas = replicas
        self._ring: list[tuple[int, str]] = []
        for w in workers:
            for i in range(replicas):
                h = int(hashlib.md5(f"{w}:{i}".encode()).hexdigest(), 16)
                self._ring.append((h, w))
        self._ring.sort()

    def worker_for(self, key: str) -> str:
        if not self._ring:
            raise ValueError("no workers in ring")
        h = int(hashlib.md5(key.encode()).hexdigest(), 16)
        for hh, w in self._ring:
            if hh >= h:
                return w
        return self._ring[0][1]


class DistributedLock:
    """Lock theo key co lease. Ban in-memory (thread-safe); production thay
    bang Redis Redlock voi cung interface acquire/release/heartbeat."""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[str, float]] = {}
        self._mu = threading.Lock()

    def acquire(self, key: str, *, owner: str, lease_seconds: int = LEASE_SECONDS) -> bool:
        now = time.monotonic()
        with self._mu:
            cur = self._locks.get(key)
            if cur and cur[1] > now:
                return False
            self._locks[key] = (owner, now + lease_seconds)
            return True

    def heartbeat(self, key: str, *, owner: str, lease_seconds: int = LEASE_SECONDS) -> bool:
        now = time.monotonic()
        with self._mu:
            cur = self._locks.get(key)
            if not cur or cur[0] != owner or cur[1] <= now:
                return False
            self._locks[key] = (owner, now + lease_seconds)
            return True

    def release(self, key: str, *, owner: str) -> None:
        with self._mu:
            if self._locks.get(key, (None, 0))[0] == owner:
                del self._locks[key]


@dataclass(order=True)
class _Queued:
    # heapq: uu tien cao truoc, roi deadline som, roi FIFO (seq).
    sort_key: tuple[int, float, int] = field(compare=True)
    job: CollectJob = field(compare=False)


def _as_utc(dt: datetime) -> datetime:
    """Chuan hoa datetime ve UTC; naive duoc coi la UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class PriorityQueue:
    """Priority queue theo (priority desc, deadline, FIFO). Dedup theo idempotency_key.

    Chi tra job DA TOI HAN (``scheduled_at <= now``) — xem fix
    ``fix_scheduler_scheduled_at_lost``. ``deadline`` o ``push()``
    chi la sort key, khac voi ``scheduled_at`` (thoi diem san sang).
    """

    def __init__(self, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._heap: list[_Queued] = []
        self._seen: dict[str, CollectJob] = {}
        self._seq = 0
        self._clock = clock

    def push(self, job: CollectJob, *, deadline: Optional[datetime] = None) -> bool:
        if job.idempotency_key in self._seen:
            return False  # dedup
        self._seq += 1
        dl = (deadline or job.scheduled_at).timestamp()
        heapq.heappush(self._heap, _Queued((-int(job.priority), dl, self._seq), job))
        self._seen[job.idempotency_key] = job
        return True

    def pop_due(self, now: Optional[datetime] = None) -> Optional[CollectJob]:
        """Lay job due tot nhat (uu tien cao nhat, roi deadline som nhat).

        Quet TOAN heap chu khong chi peek head: heap sap theo
        (priority desc, deadline, FIFO) nen job VIP tuong lai co the
        nam o dau heap trong khi job thuong da due nam phia sau —
        chi peek head se chan sai job due do. Job chua due o lai
        trong queue, khong bi mat.
        """
        if not self._heap:
            return None
        now = _as_utc(now or self._clock())
        best_idx: Optional[int] = None
        for i, item in enumerate(self._heap):
            if _as_utc(item.job.scheduled_at) <= now:
                if best_idx is None or item.sort_key < self._heap[best_idx].sort_key:
                    best_idx = i
        if best_idx is None:
            return None
        job = self._heap.pop(best_idx).job
        heapq.heapify(self._heap)
        self._seen.pop(job.idempotency_key, None)
        return job

    def pop(self, now: Optional[datetime] = None) -> Optional[CollectJob]:
        """Lay job due tiep theo. Bat buoc due — thay doi co chu y so
        voi ban cu (pop lay bat ke scheduled_at)."""
        return self.pop_due(now=now)

    def pop_any(self) -> Optional[CollectJob]:
        """Escape hatch: lay job bat ke scheduled_at. Chi dung de
        drain/test — duong chay that KHONG duoc dung ham nay."""
        if not self._heap:
            return None
        item = heapq.heappop(self._heap).job
        self._seen.pop(item.idempotency_key, None)
        return item

    def peek_due(self, now: Optional[datetime] = None) -> Optional[CollectJob]:
        """Xem job due tot nhat ma khong lay ra (None neu chua co job due)."""
        if not self._heap:
            return None
        now = _as_utc(now or self._clock())
        best: Optional[CollectJob] = None
        best_key: Optional[tuple[int, float, int]] = None
        for item in self._heap:
            if _as_utc(item.job.scheduled_at) <= now:
                if best_key is None or item.sort_key < best_key:
                    best_key = item.sort_key
                    best = item.job
        return best

    def peek(self) -> Optional[CollectJob]:
        """Xem job o dau heap ma khong lay ra (co the chua due)."""
        return self._heap[0].job if self._heap else None

    def next_scheduled_at(self) -> Optional[datetime]:
        """Thoi diem job som nhat se toi han (None neu queue rong)."""
        if not self._heap:
            return None
        return min(_as_utc(item.job.scheduled_at) for item in self._heap)

    def __len__(self) -> int:
        return len(self._heap)


class DeadLetterQueue:
    def __init__(self) -> None:
        self.jobs: list[CollectJob] = []

    def add(self, job: CollectJob) -> None:
        job.status = JobStatus.DEAD_LETTER
        self.jobs.append(job)

    def list(self) -> list[CollectJob]:
        return list(self.jobs)


@dataclass
class QuotaManager:
    """Rate-limit 3 lop: per-account, per-proxy, per-platform (token bucket).

    Diem tru duy nhat la ``try_acquire()``: kiem tra ca 3 lop truoc
    (duoi lock), roi moi tru 1 token account + 1 token platform va
    tang ``_proxy_running`` — that bai o bat ky lop nao thi khong tru
    gi ca. ``allow()`` chi la peek khong co tac dung phu, dung de
    kiem tra truoc khi quyet dinh, khong phai de tru quota.
    ``acquire_proxy_slot()`` giu lai de tuong thich nguoc cho caller
    tu quan ly slot thu cong; duong chay cua scheduler KHONG dung no
    nua (dung ``try_acquire()``), vi goi ca hai se dem proxy 2 lan.
    ``per_account_quota_per_day`` mac dinh bang
    ``per_platform_quota_per_day`` (1 account dung 1 minh co the dung
    het quota platform, nhung nhieu account cong lai van bi platform
    chan); muon gioi han rieng tung account thi truyen dict nho hon.
    """

    per_platform_quota_per_day: dict[Platform, int] = field(default_factory=lambda: {
        Platform.FACEBOOK: 200, Platform.TIKTOK: 1000, Platform.YOUTUBE: 10_000,
    })
    per_proxy_concurrency: int = 2
    per_account_quota_per_day: Optional[dict[Platform, int]] = None
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        if self.per_account_quota_per_day is None:
            self.per_account_quota_per_day = dict(self.per_platform_quota_per_day)
        self._platform_buckets = {
            p: TokenBucket(rate=q / 86400.0, capacity=float(q), clock=self.clock)
            for p, q in self.per_platform_quota_per_day.items()
        }
        self._account_buckets: dict[str, TokenBucket] = {}
        self._proxy_running: dict[str, int] = {}
        self._mu = threading.Lock()

    def _account_bucket(self, account_id: str, platform: Platform) -> TokenBucket:
        bucket = self._account_buckets.get(account_id)
        if bucket is None:
            quota = (self.per_account_quota_per_day or {}).get(
                platform, self.per_platform_quota_per_day.get(platform, 0)
            )
            bucket = TokenBucket(rate=quota / 86400.0, capacity=float(quota), clock=self.clock)
            self._account_buckets[account_id] = bucket
        return bucket

    @staticmethod
    def _has_token(bucket: TokenBucket, n: float = 1.0) -> bool:
        """Peek: co du token khong, khong tru token."""
        return bucket.wait_seconds(n) == 0.0

    def allow(self, *, account_id: str, platform: Platform, proxy_id: Optional[str]) -> bool:
        """Peek khong co tac dung phu: co cho chay 1 job moi khong?

        Kiem tra ca 3 lop nhung KHONG tru token, KHONG tang proxy.
        Dung de hien thi / quyet dinh, khong dung thay cho
        ``try_acquire()`` trong duong chay that.
        """
        with self._mu:
            if proxy_id and self._proxy_running.get(proxy_id, 0) >= self.per_proxy_concurrency:
                return False
            if not self._has_token(self._account_bucket(account_id, platform)):
                return False
            return self._has_token(self._platform_buckets[platform])

    def try_acquire(self, *, account_id: str, platform: Platform, proxy_id: Optional[str]) -> bool:
        """Nguyen tu: kiem tra 3 lop, neu du thi tru quota + giu slot.

        Tra True neu job duoc chay (da tru 1 token account, 1 token
        platform, va +1 proxy_running neu co proxy). Tra False neu
        bat ky lop nao het — khong tru gi ca. Moi lan True phai co
        dung 1 lan ``release_proxy_slot()`` tuong ung o ``finish_job``.
        """
        with self._mu:
            if proxy_id and self._proxy_running.get(proxy_id, 0) >= self.per_proxy_concurrency:
                return False
            account_bucket = self._account_bucket(account_id, platform)
            platform_bucket = self._platform_buckets[platform]
            if not self._has_token(account_bucket):
                return False
            if not self._has_token(platform_bucket):
                return False
            # Ca 3 lop deu du: bay gio moi tru, chi tru 1 lan.
            account_bucket.try_consume(1.0)
            platform_bucket.try_consume(1.0)
            if proxy_id:
                self._proxy_running[proxy_id] = self._proxy_running.get(proxy_id, 0) + 1
            return True

    def acquire_proxy_slot(self, proxy_id: str) -> bool:
        """Giu slot proxy thu cong (legacy). Scheduler dung
        ``try_acquire()`` thay vi ham nay; goi ca hai se dem 2 lan."""
        with self._mu:
            if self._proxy_running.get(proxy_id, 0) >= self.per_proxy_concurrency:
                return False
            self._proxy_running[proxy_id] = self._proxy_running.get(proxy_id, 0) + 1
            return True

    def release_proxy_slot(self, proxy_id: str) -> None:
        with self._mu:
            self._proxy_running[proxy_id] = max(0, self._proxy_running.get(proxy_id, 0) - 1)

    # Alias ro nghia hon cho release: chi giai phong slot proxy,
    # khong hoan token quota da tru (token da dung thi khong tra lai).
    release = release_proxy_slot


def _jittered_delay(base_seconds: float, rng: random.Random) -> float:
    # Backoff mu + jitter day du (full jitter): tranh dong loat retry.
    return rng.uniform(0, base_seconds)


class TaskSchedulerImpl:
    """TaskScheduler protocol (contracts) + logic queue/scheduler."""

    def __init__(self, *, workers: Optional[list[str]] = None,
                 quota: Optional[QuotaManager] = None,
                 rng: Optional[random.Random] = None,
                 clock: Callable[[], datetime] = utcnow,
                 account_status: Optional[dict[str, AccountStatus]] = None,
                 proxy_status: Optional[dict[str, ProxyStatus]] = None,
                 account_proxy: Optional[dict[str, str]] = None):
        self.workers = workers or ["worker-1"]
        self.ring = ConsistentHashRing(self.workers)
        self.quota = quota or QuotaManager()
        self.locks = DistributedLock()
        # Clock/rng phai dat TRUOC khi tao queue: moi queue dung chung
        # clock cua scheduler de pop_due() va retry scheduled_at cung
        # mot moc thoi gian (xem fix_scheduler_scheduled_at_lost).
        self._rng = rng or random.Random()
        self._clock = clock
        self.queues: dict[Platform, PriorityQueue] = {
            p: PriorityQueue(clock=clock) for p in Platform}
        self.dead_letter = DeadLetterQueue()
        # Trang thai account/proxy THAT, do store/caller dang ky hoac dong
        # bo vao (xem register_*/sync_*). Mac dinh RONG = chua biet, va
        # chua biet thi fail-closed o enqueue - KHONG mac dinh ACTIVE.
        self.account_status: dict[str, AccountStatus] = dict(account_status or {})
        self.proxy_status: dict[str, ProxyStatus] = dict(proxy_status or {})
        # account_id -> proxy_id cua proxy sticky da bind (neu co).
        self.account_proxy: dict[str, str] = dict(account_proxy or {})

    # -- Dang ky / dong bo trang thai that -------------------------------
    def set_account_status(self, account_id: str, status: AccountStatus) -> None:
        self.account_status[account_id] = status

    # Alias de caller cu/doc de hieu.
    update_account_status = set_account_status

    def set_proxy_status(self, proxy_id: str, status: ProxyStatus) -> None:
        self.proxy_status[proxy_id] = status

    update_proxy_status = set_proxy_status

    def register_account(self, account: PlatformAccount, *,
                         proxy_status: Optional[ProxyStatus] = None) -> PlatformAccount:
        """Dang ky 1 account that: trang thai + proxy sticky cua no."""
        self.account_status[account.id] = account.status
        if account.proxy_binding_id:
            self.account_proxy[account.id] = account.proxy_binding_id
            if proxy_status is not None:
                self.proxy_status[account.proxy_binding_id] = proxy_status
        return account

    def register_proxy(self, proxy: ProxyInfo) -> ProxyInfo:
        self.proxy_status[proxy.id] = proxy.status
        return proxy

    def sync_statuses(self, *, accounts: Iterable[PlatformAccount] = (),
                      proxies: Iterable[ProxyInfo] = ()) -> None:
        for account in accounts:
            self.register_account(account)
        for proxy in proxies:
            self.register_proxy(proxy)

    def sync_account_status(self, mapping: dict[str, AccountStatus]) -> None:
        """Dong bo nhanh trang thai account tu mot dict cua store that."""
        self.account_status.update(mapping)

    def sync_proxy_status(self, mapping: dict[str, ProxyStatus]) -> None:
        """Dong bo nhanh trang thai proxy tu mot dict cua store that."""
        self.proxy_status.update(mapping)

    def sync_from_store(self, store: Any) -> None:
        """Dong bo trang thai tu store that cua caller.

        Chap nhan: dict {id: PlatformAccount|ProxyInfo}, iterable cac
        PlatformAccount/ProxyInfo, hoac object co thuoc tinh ``accounts``
        / ``proxies`` (list hoac dict). Store la nguon su that - scheduler
        khong tu suy doan trang thai cua account/proxy ngoai store nay.
        """
        accounts = getattr(store, "accounts", None)
        proxies = getattr(store, "proxies", None)
        if accounts is not None or proxies is not None:
            self.sync_statuses(
                accounts=list(accounts.values()) if isinstance(accounts, dict) else list(accounts or []),
                proxies=list(proxies.values()) if isinstance(proxies, dict) else list(proxies or []))
            return
        items = list(store.values()) if isinstance(store, dict) else list(store)
        self.sync_statuses(
            accounts=[x for x in items if isinstance(x, PlatformAccount)],
            proxies=[x for x in items if isinstance(x, ProxyInfo)])

    # -- Cong fail-closed dung chung cho enqueue / requeue ----------------
    def _resolve_proxy_id(self, job: CollectJob,
                          proxy_id: Optional[str] = None) -> Optional[str]:
        if proxy_id:
            return proxy_id
        payload = job.payload or {}
        for key in ("proxy_id", "proxy_binding_id"):
            if payload.get(key):
                return str(payload[key])
        return self.account_proxy.get(job.account_id)

    def _gate_reason(self, job: CollectJob, *,
                     proxy_id: Optional[str] = None) -> Optional[str]:
        """None = duoc enqueue. Chuoi = ly do REJECTED (fail-closed)."""
        status = self.account_status.get(job.account_id)
        if status is None:
            return "unknown account status - fail-closed"
        if is_fail_closed_status(status):
            return f"fail-closed account status: {status.value}"
        pid = self._resolve_proxy_id(job, proxy_id)
        if pid is not None:
            pstatus = self.proxy_status.get(pid)
            if pstatus is None:
                return "unknown proxy status - fail-closed"
            if pstatus not in _USABLE_PROXY_STATUSES:
                return f"proxy not usable: {pstatus.value}"
        return None

    # -- TaskScheduler protocol -------------------------------------------
    def enqueue(self, job: CollectJob, *,
                account_status: Optional[AccountStatus] = None,
                proxy_id: Optional[str] = None,
                proxy_status: Optional[ProxyStatus] = None) -> CollectJob:
        """Dua job vao queue - fail-closed voi trang thai chua biet.

        Caller co the truyen trang thai tuong minh cho lan enqueue nay
        (duoc ghi nhan nhu mot lan dong bo tu store that); neu khong,
        scheduler chi tin trang thai da dang ky/dong bo truoc do.
        """
        if account_status is not None:
            self.account_status[job.account_id] = account_status
        if proxy_status is not None:
            pid = self._resolve_proxy_id(job, proxy_id)
            if pid is not None:
                self.proxy_status[pid] = proxy_status
                self.account_proxy.setdefault(job.account_id, pid)
        reason = self._gate_reason(job, proxy_id=proxy_id)
        if reason is not None:
            job.status = JobStatus.REJECTED
            job.last_error = reason
            return job
        # Queue rieng theo platform; job mac dinh PENDING.
        job.status = JobStatus.PENDING
        self.queues[job.platform].push(job)
        return job

    def requeue_dead_letter(self, job_id: str) -> CollectJob:
        for i, job in enumerate(self.dead_letter.jobs):
            if job.id == job_id:
                # Requeue cung la mot lan enqueue: cung phai qua cong
                # fail-closed (account co the da BANNED tu luc job chet).
                reason = self._gate_reason(job)
                del self.dead_letter.jobs[i]
                if reason is not None:
                    job.status = JobStatus.REJECTED
                    job.last_error = reason
                    return job
                job.attempts = 0
                job.status = JobStatus.PENDING
                self.queues[job.platform].push(job)
                return job
        raise KeyError(f"job {job_id} not in dead-letter queue")

    # -- Worker API --------------------------------------------------------
    def worker_for_account(self, account_id: str) -> str:
        return self.ring.worker_for(account_id)

    def acquire_job(self, *, worker_id: str, platform: Platform,
                    proxy_id: Optional[str] = None) -> Optional[CollectJob]:
        """Lay job tiep theo cho worker, ton trong quota + lock per-account.

        Quota chi tru 1 lan, o 1 diem duy nhat: ``quota.try_acquire()``.
        Khong goi rieng ``acquire_proxy_slot()`` roi ``allow()`` nua
        (do la goc cua loi off-by-one). Neu try_acquire that bai thi
        chua tru gi ca nen khong can release slot.

        Chi lay job DA TOI HAN (``scheduled_at <= now``) qua
        ``pop_due()`` — job hen tuong lai o lai trong queue. ``now``
        duoc snapshot 1 lan moi lan goi de moi job trong vong lap
        dung cung mot moc thoi gian.
        """
        queue = self.queues[platform]
        now = self._clock()
        # Thu lan luot (queue nho trong test/dev; production dung peek/pop co dieu kien).
        tried: list[CollectJob] = []
        while len(queue):
            job = queue.pop_due(now=now)
            if job is None:
                break
            assert job is not None
            if self.ring.worker_for(job.account_id) != worker_id:
                tried.append(job)
                continue
            if not self.locks.acquire(f"account:{job.account_id}", owner=job.id):
                tried.append(job)
                continue
            if not self.quota.try_acquire(account_id=job.account_id, platform=platform, proxy_id=proxy_id):
                self.locks.release(f"account:{job.account_id}", owner=job.id)
                tried.append(job)
                continue
            job.status = JobStatus.RUNNING
            job.attempts += 1
            job.lease_until = self._clock() + timedelta(seconds=LEASE_SECONDS)
            for j in tried:
                self.queues[platform].push(j)
            return job
        for j in tried:
            self.queues[platform].push(j)
        return None

    def heartbeat(self, job: CollectJob) -> bool:
        return self.locks.heartbeat(f"account:{job.account_id}", owner=job.id)

    def finish_job(self, job: CollectJob, *, ok: bool, error: str = "",
                   proxy_id: Optional[str] = None) -> JobStatus:
        self.locks.release(f"account:{job.account_id}", owner=job.id)
        if proxy_id:
            self.quota.release_proxy_slot(proxy_id)
        if ok:
            job.status = JobStatus.SUCCEEDED
            return job.status
        job.last_error = error
        if job.can_retry:
            job.status = JobStatus.RETRYING
            # Backoff mu + jitter, enqueue lai sau delay (don gian: scheduled_at tang).
            delay = _jittered_delay(2 ** job.attempts * 5.0, self._rng)
            job.scheduled_at = self._clock() + timedelta(seconds=delay)
            self.queues[job.platform].push(job)
        else:
            self.dead_letter.add(job)
        return job.status

    def reject_job(self, job: CollectJob, *, reason: str) -> CollectJob:
        job.status = JobStatus.REJECTED
        job.last_error = reason
        return job


def schedule_jobs_for_account(account: PlatformAccount, *, videos: list[dict[str, Any]],
                              now: Optional[datetime] = None,
                              vip: bool = False) -> list[CollectJob]:
    """Lap lich sync: video moi (<24h) 15 phut, video cu 6-12h, VIP uu tien.

    ``videos``: [{video_id, published_at, is_new}] — scheduler khong goi API.
    """
    now = now or utcnow()
    jobs: list[CollectJob] = []
    priority = JobPriority.VIP if vip else JobPriority.NORMAL
    # 1 job SYNC_VIDEOS cho account, roi 1 job FETCH_STATS / video.
    jobs.append(CollectJob(
        id=uuid.uuid4().hex, job_type=JobType.SYNC_VIDEOS, account_id=account.id,
        platform=account.platform, priority=priority, scheduled_at=now,
    ))
    for v in videos:
        is_new = bool(v.get("is_new")) or (
            v.get("published_at") and (now - v["published_at"]) < timedelta(hours=24)
        )
        interval = NEW_VIDEO_INTERVAL if is_new else OLD_VIDEO_INTERVAL
        jobs.append(CollectJob(
            id=uuid.uuid4().hex, job_type=JobType.FETCH_STATS, account_id=account.id,
            platform=account.platform, video_id=str(v.get("video_id") or ""),
            priority=JobPriority.HIGH if is_new else priority,
            scheduled_at=now + interval,
        ))
    return jobs
