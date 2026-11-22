"""fix_scheduler_dead_letter_test_clock — test dead-letter dung FakeClock.

Loi o TEST (``tests/test_systems_arch_m2.py``), code scheduler dung:
``finish_job(ok=False)`` dat job RETRYING voi ``scheduled_at = now +
backoff``; ``pop_due`` chi tra job DA TOI HAN (fix scheduled_at da
khoa); ``attempts`` chi tang trong ``acquire_job``. Test cu dung dong
ho that + fallback ``or got``: acquire tra None (job chua due) roi
finish lai cung mot object voi attempts mai 1, ``can_retry`` mai
dung, trang thai cuoi RETRYING thay vi DEAD_LETTER. Tuyet doi khong
noi ``pop_due`` de test cu xanh.

File nay la ban kiem chung chay duoc tren VM cho test da viet lai
trong ``test_systems_arch_m2.py`` (file do import them core/models,
facebook_connector, monitor, api/main, fastapi — khong co tren VM
nay): test chinh o day la CUNG scenario, CUNG FakeClock, CUNG
assertion nhu test da viet lai, nen pass o day la bang chung truc
tiep cho test do. Them cac test bien pin dac ta da do: job chay toi
da 3 lan tong cong truoc DLQ (``can_retry = attempts <
MAX_JOB_RETRIES``); retry khong acquire duoc truoc khi het backoff;
finish lai job da chet se add trung vao dead-letter (ly do vong lap
PHAI dung ngay khi DEAD_LETTER); vong lap cu (khong clock) ket thuc
o RETRYING — chung minh test cu dang kiem tra hanh vi buggy.

Stdlib + pytest only. Chay tu repo root:
pytest -q tests/test_fix_scheduler_dead_letter_test_clock.py
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import (  # noqa: E402
    MAX_JOB_RETRIES,
    AccountStatus,
    CollectJob,
    JobStatus,
    JobType,
    Platform,
)
from src.scheduler.task_scheduler import TaskSchedulerImpl  # noqa: E402

UTC = timezone.utc
T0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


class _FakeClock:
    """Clock dieu khien duoc: tra ve thoi gian gia lap, tua duoc qua
    backoff retry. Scheduler + PriorityQueue dung chung clock nay nen
    pop_due() va scheduled_at retry cung mot moc thoi gian."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, delta):
        self.now += delta


def _scheduler(clock):
    sched = TaskSchedulerImpl(
        workers=["worker-1"], rng=random.Random(0), clock=clock,
    )
    sched.account_status["acc1"] = AccountStatus.ACTIVE
    return sched


def _run_dead_letter_flow(sched, clock):
    """Chay dung flow cua test da viet lai trong test_systems_arch_m2.py:
    acquire lan dau, roi vong lap co gioi han: finish fail, dung ngay
    khi DEAD_LETTER, neu chua thi tua clock qua backoff roi acquire lai
    (assert acquire tra ve job, khong None). Tra (status, job)."""
    got = sched.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK)
    assert got is not None and got.id == "j1" and got.status == JobStatus.RUNNING
    st = None
    for _ in range(MAX_JOB_RETRIES + 2):
        st = sched.finish_job(got, ok=False, error="x")
        if st == JobStatus.DEAD_LETTER:
            break
        clock.advance(timedelta(seconds=60))
        got = sched.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK)
        assert got is not None, "retry job phai acquire lai duoc sau backoff"
        assert got.id == "j1"
    else:
        raise AssertionError("job khong vao DEAD_LETTER trong gioi han vong lap")
    return st, got


def test_dead_letter_with_fake_clock_matches_rewritten_m2_test():
    # Cung scenario nhu test_scheduler_priority_and_dedup_and_dead_letter
    # da viet lai: j1 chay that, j2 tach rieng (video + scheduled_at
    # khac, hen xa +1h) nen khong bi dedup va khong canh tranh acquire.
    clock = _FakeClock(T0)
    s = _scheduler(clock)
    job = CollectJob(id="j1", job_type=JobType.FETCH_STATS, account_id="acc1",
                     platform=Platform.FACEBOOK, scheduled_at=T0)
    assert s.enqueue(job).status == JobStatus.PENDING
    job2 = CollectJob(id="j2", job_type=JobType.FETCH_STATS, account_id="acc1",
                      platform=Platform.FACEBOOK, video_id="vid-2",
                      scheduled_at=T0 + timedelta(hours=1))
    assert s.enqueue(job2).status == JobStatus.PENDING
    assert len(s.queues[Platform.FACEBOOK]) == 2
    st, got = _run_dead_letter_flow(s, clock)
    assert st == JobStatus.DEAD_LETTER
    assert got.attempts == MAX_JOB_RETRIES
    assert len(s.dead_letter.list()) == 1
    assert len(s.queues[Platform.FACEBOOK]) == 1  # j2 van nguyen
    back = s.requeue_dead_letter(got.id)
    assert back.status == JobStatus.PENDING


def test_execution_count_is_exactly_max_job_retries():
    # Dac ta da do, bac bo off-by-one cua backend-sec: job vao DLQ sau
    # dung 3 lan acquire+fail (3 lan chay tong cong), khong co luot
    # chay thu 4 — acquire tiep tra None.
    clock = _FakeClock(T0)
    s = _scheduler(clock)
    s.enqueue(CollectJob(id="j1", job_type=JobType.FETCH_STATS, account_id="acc1",
                         platform=Platform.FACEBOOK, scheduled_at=T0))
    runs = 0
    st = None
    got = s.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK)
    assert got is not None
    while got is not None:
        runs += 1
        st = s.finish_job(got, ok=False, error="x")
        if st == JobStatus.DEAD_LETTER:
            break
        clock.advance(timedelta(seconds=60))
        got = s.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK)
    assert runs == MAX_JOB_RETRIES == 3
    assert st == JobStatus.DEAD_LETTER
    clock.advance(timedelta(seconds=3600))
    assert s.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK) is None


def test_retry_not_acquirable_before_backoff_elapses():
    # Pin hanh vi scheduled_at ma test cu da vi pham: ngay sau khi fail,
    # acquire tra None cho den khi clock tua qua backoff.
    clock = _FakeClock(T0)
    s = _scheduler(clock)
    s.enqueue(CollectJob(id="j1", job_type=JobType.FETCH_STATS, account_id="acc1",
                         platform=Platform.FACEBOOK, scheduled_at=T0))
    got = s.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK)
    assert got is not None
    assert s.finish_job(got, ok=False, error="x") == JobStatus.RETRYING
    assert s.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK) is None
    clock.advance(timedelta(seconds=60))
    got2 = s.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK)
    assert got2 is not None and got2.id == "j1" and got2.attempts == 2


def test_finish_dead_job_twice_adds_it_twice():
    # Dac ta hanh vi hien tai (khong phai hop dong ly tuong): finish_job
    # khong chan job da DEAD_LETTER nen finish lai se add trung vao
    # dead-letter. Day la ly do vong lap PHAI dung ngay khi DEAD_LETTER
    # — duong chay that khong bao gio finish job ma worker khong acquire.
    clock = _FakeClock(T0)
    s = _scheduler(clock)
    s.enqueue(CollectJob(id="j1", job_type=JobType.FETCH_STATS, account_id="acc1",
                         platform=Platform.FACEBOOK, scheduled_at=T0))
    st, got = _run_dead_letter_flow(s, clock)
    assert st == JobStatus.DEAD_LETTER
    assert len(s.dead_letter.list()) == 1
    assert s.finish_job(got, ok=False, error="x") == JobStatus.DEAD_LETTER
    assert len(s.dead_letter.list()) == 2


def test_old_loop_without_clock_ends_retrying():
    # Tai hien loi cu: vong lap cu (dong ho that, fallback `or got`,
    # hardcode 3 lan) ket thuc o RETRYING — chung minh test cu dang
    # kiem tra hanh vi buggy, khong phai scheduler sai.
    s = TaskSchedulerImpl(workers=["worker-1"], rng=random.Random(0))
    s.account_status["acc1"] = AccountStatus.ACTIVE
    job = CollectJob(id="j1", job_type=JobType.FETCH_STATS, account_id="acc1",
                     platform=Platform.FACEBOOK)
    assert s.enqueue(job).status == JobStatus.PENDING
    job2 = CollectJob(id="j2", job_type=JobType.FETCH_STATS, account_id="acc1",
                      platform=Platform.FACEBOOK)
    assert s.enqueue(job2).status == JobStatus.PENDING
    got = s.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK)
    assert got is not None and got.status == JobStatus.RUNNING
    st = None
    for _ in range(3):
        got = s.acquire_job(worker_id="worker-1", platform=Platform.FACEBOOK) or got
        st = s.finish_job(got, ok=False, error="x")
    assert st == JobStatus.RETRYING
    assert got.attempts == 1  # attempts khong bao gio tang: acquire tra None
    assert len(s.dead_letter.list()) == 0
