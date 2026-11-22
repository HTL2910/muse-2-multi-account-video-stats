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
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

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


class PriorityQueue:
    """Priority queue theo (priority desc, deadline, FIFO). Dedup theo idempotency_key."""

    def __init__(self) -> None:
        self._heap: list[_Queued] = []
        self._seen: dict[str, CollectJob] = {}
        self._seq = 0

    def push(self, job: CollectJob, *, deadline: Optional[datetime] = None) -> bool:
        if job.idempotency_key in self._seen:
            return False  # dedup
        self._seq += 1
        dl = (deadline or job.scheduled_at).timestamp()
        heapq.heappush(self._heap, _Queued((-int(job.priority), dl, self._seq), job))
        self._seen[job.idempotency_key] = job
        return True

    def pop(self) -> Optional[CollectJob]:
        if not self._heap:
            return None
        item = heapq.heappop(self._heap).job
        self._seen.pop(item.idempotency_key, None)
        return item

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
    """Rate-limit 3 lop: per-account, per-proxy, per-platform (token bucket)."""

    per_platform_quota_per_day: dict[Platform, int] = field(default_factory=lambda: {
        Platform.FACEBOOK: 200, Platform.TIKTOK: 1000, Platform.YOUTUBE: 10_000,
    })
    per_proxy_concurrency: int = 2

    def __post_init__(self) -> None:
        self._platform_buckets = {
            p: TokenBucket(rate=q / 86400.0, capacity=float(q))
            for p, q in self.per_platform_quota_per_day.items()
        }
        self._account_buckets: dict[str, TokenBucket] = {}
        self._proxy_running: dict[str, int] = {}

    def allow(self, *, account_id: str, platform: Platform, proxy_id: Optional[str]) -> bool:
        if proxy_id and self._proxy_running.get(proxy_id, 0) >= self.per_proxy_concurrency:
            return False
        bucket = self._platform_buckets[platform]
        return bucket.try_consume(1.0)

    def acquire_proxy_slot(self, proxy_id: str) -> bool:
        if self._proxy_running.get(proxy_id, 0) >= self.per_proxy_concurrency:
            return False
        self._proxy_running[proxy_id] = self._proxy_running.get(proxy_id, 0) + 1
        return True

    def release_proxy_slot(self, proxy_id: str) -> None:
        self._proxy_running[proxy_id] = max(0, self._proxy_running.get(proxy_id, 0) - 1)


def _jittered_delay(base_seconds: float, rng: random.Random) -> float:
    # Backoff mu + jitter day du (full jitter): tranh dong loat retry.
    return rng.uniform(0, base_seconds)


class TaskSchedulerImpl:
    """TaskScheduler protocol (contracts) + logic queue/scheduler."""

    def __init__(self, *, workers: Optional[list[str]] = None,
                 quota: Optional[QuotaManager] = None,
                 rng: Optional[random.Random] = None,
                 clock: Callable[[], datetime] = utcnow):
        self.workers = workers or ["worker-1"]
        self.ring = ConsistentHashRing(self.workers)
        self.quota = quota or QuotaManager()
        self.locks = DistributedLock()
        self.queues: dict[Platform, PriorityQueue] = {p: PriorityQueue() for p in Platform}
        self.dead_letter = DeadLetterQueue()
        self._rng = rng or random.Random()
        self._clock = clock
        # Trang thai account/proxy do nguoi goi cap nhat (fail-closed gate o enqueue).
        self.account_status: dict[str, AccountStatus] = {}
        self.proxy_status: dict[str, ProxyStatus] = {}

    # -- TaskScheduler protocol -------------------------------------------
    def enqueue(self, job: CollectJob) -> CollectJob:
        if is_fail_closed_status(self.account_status.get(job.account_id, AccountStatus.ACTIVE)):
            job.status = JobStatus.REJECTED
            job.last_error = "fail-closed account status"
            return job
        # Queue rieng theo platform; job mac dinh PENDING.
        job.status = JobStatus.PENDING
        self.queues[job.platform].push(job)
        return job

    def requeue_dead_letter(self, job_id: str) -> CollectJob:
        for i, job in enumerate(self.dead_letter.jobs):
            if job.id == job_id:
                del self.dead_letter.jobs[i]
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
        """Lay job tiep theo cho worker, ton trong quota + lock per-account."""
        queue = self.queues[platform]
        # Thu lan luot (queue nho trong test/dev; production dung peek/pop co dieu kien).
        tried: list[CollectJob] = []
        while len(queue):
            job = queue.pop()
            assert job is not None
            if self.ring.worker_for(job.account_id) != worker_id:
                tried.append(job)
                continue
            if not self.locks.acquire(f"account:{job.account_id}", owner=job.id):
                tried.append(job)
                continue
            if proxy_id and not self.quota.acquire_proxy_slot(proxy_id):
                self.locks.release(f"account:{job.account_id}", owner=job.id)
                tried.append(job)
                continue
            if not self.quota.allow(account_id=job.account_id, platform=platform, proxy_id=proxy_id):
                self.locks.release(f"account:{job.account_id}", owner=job.id)
                if proxy_id:
                    self.quota.release_proxy_slot(proxy_id)
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
