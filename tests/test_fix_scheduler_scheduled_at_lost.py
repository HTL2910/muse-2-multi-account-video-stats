"""fix_scheduler_scheduled_at_lost — khoi phuc fix scheduled_at bi mat khi merge.

Ban fix goc (fix_scheduler_scheduled_at) da duoc resubmit va PASS tren VM
nhung khong len main: PriorityQueue tren main lai pop() bat ke
scheduled_at, va file test cu cung vang mat khoi tests/ tren main.
File nay khoi phuc nguyen 8 test cu, doi ten theo nhanh moi, de
lan merge nay co the kiem chung bang manifest (file test phai xuat
hien trong tests/ tren main) chu khong chi dua vao bao cao nop bai.

fix_scheduler_scheduled_at — chi chay job da toi han scheduled_at.

Kiem tra loi cu: PriorityQueue.pop() / acquire_job() lay job bat ke
scheduled_at, nen FETCH_STATS video cu (+6h), video moi (+15 phut)
va job retry sau backoff deu bi chay ngay lap tuc, dot quota.

Test dung fake clock (dong bang thoi gian) de khong phu thuoc
thoi gian thuc:
- job +6h khong duoc acquire truoc 6h, van nam trong queue, acquire
  duoc sau 6h.
- job +15 phut (video moi) giu dung cadence tuong tu.
- job retry sau 429 khong duoc acquire truoc delay backoff.
- job VIP tuong lai khong chan job thuong da toi han (heap sap theo
  uu tien, khong phai theo scheduled_at — chi peek dau heap la sai).
- job da toi han van duoc acquire binh thuong (khong regression).

Stdlib + pytest only. Chay tu repo root:
pytest -q tests/test_fix_scheduler_scheduled_at_lost.py
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import (  # noqa: E402
    AccountStatus,
    CollectJob,
    JobPriority,
    JobStatus,
    JobType,
    Platform,
    PlatformAccount,
)
from src.scheduler.task_scheduler import (  # noqa: E402
    OLD_VIDEO_INTERVAL,
    NEW_VIDEO_INTERVAL,
    PriorityQueue,
    TaskSchedulerImpl,
    schedule_jobs_for_account,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


class FakeClock:
    """Dong ho co the tua nhanh de test cadence ma khong doi thoi gian thuc."""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


def _account() -> PlatformAccount:
    return PlatformAccount(
        id="acc-1", workspace_id="ws-1", platform=Platform.TIKTOK,
        platform_user_id="u1", status=AccountStatus.ACTIVE,
    )


def _job(job_id: str, scheduled_at: datetime, priority=JobPriority.NORMAL,
         video_id: str = "vid-1") -> CollectJob:
    # video_id khac nhau cho moi job trong test uu tien: contracts v1.1.0
    # sinh idempotency_key tu (job_type, account, video, phut lap lich),
    # nen 2 job cung video + cung phut se bi dedup dung nghia.
    return CollectJob(
        id=job_id, job_type=JobType.FETCH_STATS, account_id="acc-1",
        platform=Platform.TIKTOK, video_id=video_id,
        priority=priority, scheduled_at=scheduled_at,
    )


def _scheduler(clock: FakeClock) -> TaskSchedulerImpl:
    sched = TaskSchedulerImpl(
        workers=["worker-1"], clock=clock, rng=random.Random(0),
    )
    sched.set_account_status("acc-1", AccountStatus.ACTIVE)
    return sched


# --- PriorityQueue ---------------------------------------------------------

def test_pop_due_returns_none_for_future_job_and_keeps_it():
    clock = FakeClock(T0)
    q = PriorityQueue(clock=clock)
    q.push(_job("j1", T0 + timedelta(hours=6)))
    assert q.pop_due() is None
    assert q.pop() is None  # pop() mac dinh cung ton trong scheduled_at
    assert len(q) == 1
    assert q.next_scheduled_at() == T0 + timedelta(hours=6)
    assert q.peek_due() is None
    # Tua toi han: lay duoc ngay.
    clock.advance(timedelta(hours=6))
    assert q.pop_due().id == "j1"
    assert len(q) == 0


def test_pop_due_skips_future_vip_and_returns_due_normal_job():
    # Loi tinh te: heap sap theo uu tien, job VIP tuong lai o dau heap.
    # Chi peek dau heap roi tra None la sai — job thuong da toi han
    # o sau van phai duoc chay.
    q = PriorityQueue(clock=FakeClock(T0))
    q.push(_job("due-normal", T0, priority=JobPriority.NORMAL))
    q.push(_job("future-vip", T0 + timedelta(hours=6), priority=JobPriority.VIP))
    got = q.pop_due()
    assert got is not None and got.id == "due-normal"
    assert len(q) == 1
    assert q.peek().id == "future-vip"


def test_pop_due_respects_priority_among_due_jobs():
    q = PriorityQueue(clock=FakeClock(T0))
    q.push(_job("low", T0, priority=JobPriority.LOW, video_id="vid-low"))
    q.push(_job("high", T0, priority=JobPriority.HIGH, video_id="vid-high"))
    assert q.pop_due().id == "high"
    assert q.pop_due().id == "low"


def test_pop_any_is_the_explicit_escape_hatch():
    q = PriorityQueue(clock=FakeClock(T0))
    q.push(_job("future", T0 + timedelta(hours=6)))
    assert q.pop_any().id == "future"
    assert len(q) == 0


# --- acquire_job / cadence -------------------------------------------------

def test_old_video_fetch_stats_not_acquired_before_6h():
    clock = FakeClock(T0)
    sched = _scheduler(clock)
    account = _account()
    jobs = schedule_jobs_for_account(
        account, videos=[{"video_id": "old-1", "is_new": False}], now=T0,
    )
    assert len(jobs) == 2
    sync_job, stats_job = jobs
    assert stats_job.scheduled_at == T0 + OLD_VIDEO_INTERVAL
    for job in jobs:
        assert sched.enqueue(job).status == JobStatus.PENDING

    # Tai T0: chi SYNC_VIDEOS (scheduled_at = now) duoc acquire.
    got = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK)
    assert got is not None and got.id == sync_job.id
    # Hoan thanh SYNC_VIDEOS de giai phong lock per-account (concurrency=1),
    # neu khong FETCH_STATS cung account se bi chan boi lock chu khong
    # phai boi scheduled_at — test se do loi cho sai nguyen nhan.
    assert sched.finish_job(got, ok=True) == JobStatus.SUCCEEDED
    assert sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK) is None
    assert len(sched.queues[Platform.TIKTOK]) == 1  # FETCH_STATS van nam lai

    # Tua 5h59: van chua toi han.
    clock.advance(timedelta(hours=5, minutes=59))
    assert sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK) is None
    # Tua them 1 phut (du 6h): acquire duoc.
    clock.advance(timedelta(minutes=1))
    got = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK)
    assert got is not None and got.id == stats_job.id


def test_new_video_fetch_stats_keeps_15_minute_cadence():
    clock = FakeClock(T0)
    sched = _scheduler(clock)
    account = _account()
    jobs = schedule_jobs_for_account(
        account, videos=[{"video_id": "new-1", "is_new": True}], now=T0,
    )
    stats_job = jobs[1]
    assert stats_job.scheduled_at == T0 + NEW_VIDEO_INTERVAL
    for job in jobs:
        sched.enqueue(job)
    # Lay SYNC_VIDEOS truoc, hoan thanh de giai phong lock per-account,
    # roi FETCH_STATS chua toi han.
    sync_got = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK)
    assert sync_got is not None and sync_got.id == jobs[0].id
    assert sched.finish_job(sync_got, ok=True) == JobStatus.SUCCEEDED
    assert sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK) is None
    clock.advance(timedelta(minutes=14))
    assert sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK) is None
    clock.advance(timedelta(minutes=1))
    got = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK)
    assert got is not None and got.id == stats_job.id


def test_retry_after_429_not_acquired_before_backoff():
    clock = FakeClock(T0)
    sched = _scheduler(clock)
    job = _job("j1", T0)
    sched.enqueue(job)
    got = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK)
    assert got is not None and got.id == "j1"
    # That bai (vd 429): retry voi scheduled_at = now + delay backoff.
    status = sched.finish_job(got, ok=False, error="429 rate limited")
    assert status == JobStatus.RETRYING
    assert got.scheduled_at > T0
    # Ngay lap tuc: khong acquire duoc job retry.
    assert sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK) is None
    assert len(sched.queues[Platform.TIKTOK]) == 1
    # Tua qua delay toi da (2**1 * 5 = 10s, jitter <= delay): acquire duoc.
    clock.advance(timedelta(seconds=60))
    got2 = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK)
    assert got2 is not None and got2.id == "j1"


def test_due_job_still_acquired_normally_no_regression():
    clock = FakeClock(T0)
    sched = _scheduler(clock)
    job = _job("j1", T0)
    sched.enqueue(job)
    got = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK)
    assert got is not None and got.id == "j1"
    assert got.status == JobStatus.RUNNING
    assert sched.finish_job(got, ok=True) == JobStatus.SUCCEEDED
