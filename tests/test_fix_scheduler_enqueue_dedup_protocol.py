"""fix_scheduler_enqueue_dedup_protocol — enqueue() tra job goc kem co dedup.

Loi CODE tren main (contracts 1.1.0): ``TaskSchedulerImpl.enqueue()`` goi
``PriorityQueue.push()`` nhung bo qua gia tri tra ve. Push tra False khi
``idempotency_key`` da co trong ``_seen`` (job khong vao heap), nhung
enqueue van gan ``job.status = PENDING`` va tra job do ve — caller tin
job da xep hang trong khi job da bi loai lang le (mat job) va khong ai
nhan duoc co dedup. ``CollectJob`` tu sinh key theo bucket phut cua
``scheduled_at`` nen hai job cung video trong cung mot phut la trung
key that (test m2 da chung minh: j2 bi loai khoi queue nhung van duoc
bao PENDING).

Fix (merge nhanh fix_scheduler_enqueue_dedup da nop, khong viet moi):
push False thi tra job GOC — DUNG object da queue — kem
``deduplicated=True`` / ``duplicate_of=<id goc>``; push thanh cong thi
giu hanh vi cu. Khong dung ``dataclasses.replace()``: ban sao tach roi
khoi job trong queue, sai tinh than "tra ve job goc". ``PriorityQueue``
expose tra cuu goc theo key qua ``get_by_idempotency_key()``.

Stdlib + pytest only. Chay tu repo root:
pytest -q tests/test_fix_scheduler_enqueue_dedup_protocol.py
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
)
from src.scheduler.task_scheduler import PriorityQueue, TaskSchedulerImpl  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


def _scheduler() -> TaskSchedulerImpl:
    sched = TaskSchedulerImpl(
        workers=["worker-1"], clock=lambda: T0, rng=random.Random(0),
    )
    sched.set_account_status("acc-1", AccountStatus.ACTIVE)
    return sched


def _job(job_id: str, scheduled_at: datetime, video_id: str = "vid-1",
         priority=JobPriority.NORMAL) -> CollectJob:
    return CollectJob(
        id=job_id, job_type=JobType.FETCH_STATS, account_id="acc-1",
        platform=Platform.TIKTOK, video_id=video_id,
        priority=priority, scheduled_at=scheduled_at,
    )


def test_enqueue_duplicate_same_minute_returns_original_with_dedup_flag():
    # Test chinh theo phuong an hoi dong: 2 job cung key trong cung phut.
    sched = _scheduler()
    first = _job("job-1", T0)
    dup = _job("job-2", T0)
    assert first.idempotency_key == dup.idempotency_key

    r1 = sched.enqueue(first)
    assert r1 is first
    assert r1.status == JobStatus.PENDING
    assert r1.deduplicated is False
    assert r1.duplicate_of is None

    r2 = sched.enqueue(dup)
    assert r2 is first, "phai tra DUNG object job goc, khong phai ban sao"
    assert r2.deduplicated is True
    assert r2.duplicate_of == "job-1"
    assert r2.status == JobStatus.PENDING  # job goc van dang xep hang
    assert len(sched.queues[Platform.TIKTOK]) == 1


def test_duplicate_never_runs_original_still_runs_once():
    sched = _scheduler()
    sched.enqueue(_job("job-1", T0))
    sched.enqueue(_job("job-2", T0))
    got = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK)
    assert got is not None and got.id == "job-1"
    assert sched.finish_job(got, ok=True) == JobStatus.SUCCEEDED
    assert len(sched.queues[Platform.TIKTOK]) == 0
    assert sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK) is None


def test_enqueue_different_minute_is_not_a_duplicate():
    sched = _scheduler()
    r1 = sched.enqueue(_job("job-1", T0))
    r2 = sched.enqueue(_job("job-2", T0 + timedelta(minutes=1)))
    assert r1.idempotency_key != r2.idempotency_key
    assert r1 is not r2
    assert r2.status == JobStatus.PENDING
    assert r2.deduplicated is False
    assert r2.duplicate_of is None
    assert len(sched.queues[Platform.TIKTOK]) == 2


def test_enqueue_different_video_same_minute_is_not_a_duplicate():
    sched = _scheduler()
    sched.enqueue(_job("job-1", T0, video_id="vid-1"))
    r2 = sched.enqueue(_job("job-2", T0, video_id="vid-2"))
    assert r2.status == JobStatus.PENDING
    assert r2.deduplicated is False
    assert len(sched.queues[Platform.TIKTOK]) == 2


def test_enqueue_duplicate_keeps_original_priority_and_schedule():
    # Job trung lap khong duoc ghi de uu tien / lich cua job goc.
    sched = _scheduler()
    sched.enqueue(_job("job-1", T0, priority=JobPriority.HIGH))
    r2 = sched.enqueue(_job("job-2", T0, priority=JobPriority.LOW))
    assert r2.id == "job-1"
    assert r2.priority == JobPriority.HIGH
    assert r2.scheduled_at == T0


def test_enqueue_same_key_after_original_popped_is_not_dedup():
    # Key chi bi chan khi job goc con trong queue: job goc da chay
    # xong thi key duoc giai phong, job moi cung key duoc xep hang.
    sched = _scheduler()
    sched.enqueue(_job("job-1", T0))
    got = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK)
    assert got is not None and got.id == "job-1"
    assert sched.finish_job(got, ok=True) == JobStatus.SUCCEEDED
    r2 = sched.enqueue(_job("job-2", T0))
    assert r2.id == "job-2"
    assert r2.status == JobStatus.PENDING
    assert r2.deduplicated is False
    assert len(sched.queues[Platform.TIKTOK]) == 1


def test_rejected_job_never_reaches_dedup_path():
    # Cong fail-closed chay truoc push: account chua biet -> REJECTED,
    # khong bi nham thanh dedup va khong lam ban _seen.
    sched = TaskSchedulerImpl(
        workers=["worker-1"], clock=lambda: T0, rng=random.Random(0),
    )
    unknown = CollectJob(
        id="job-x", job_type=JobType.FETCH_STATS, account_id="acc-unknown",
        platform=Platform.TIKTOK, video_id="vid-1", scheduled_at=T0,
    )
    r = sched.enqueue(unknown)
    assert r.status == JobStatus.REJECTED
    assert r.deduplicated is False
    assert len(sched.queues[Platform.TIKTOK]) == 0


def test_get_by_idempotency_key_accessor():
    # Accessor moi cua PriorityQueue: tra job da queue theo key,
    # None khi key chua co hoac job da bi pop ra.
    q: PriorityQueue = PriorityQueue(clock=lambda: T0)
    job = _job("job-1", T0)
    assert q.get_by_idempotency_key(job.idempotency_key) is None
    assert q.push(job) is True
    assert q.get_by_idempotency_key(job.idempotency_key) is job
    assert q.pop_due(now=T0) is job
    assert q.get_by_idempotency_key(job.idempotency_key) is None
