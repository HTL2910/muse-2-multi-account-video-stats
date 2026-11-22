"""fix_scheduler_quota_proxy_per_account — quota chi tru 1 lan, per-account that.

Loi cu: acquire_job goi acquire_proxy_slot() (tang _proxy_running)
roi goi allow() (lai kiem tra _proxy_running >= limit) nen gioi han
thuc luon bang cau hinh tru 1 (limit=1 thi khong job nao chay);
_account_buckets duoc tao nhung allow() khong bao gio dung.

Test:
- per_proxy_concurrency=1: try_acquire chay duoc dung 1 job, job thu
  2 bi chan, release xong chay lai duoc.
- per_proxy_concurrency=2: chay duoc dung 2 job dong thoi, job thu
  3 bi chan.
- allow() la peek: goi nhieu lan khong tru token, khong tang proxy.
- per-account that: account A het quota rieng thi account B van chay
  duoc (platform chua het); platform het thi ca 2 deu bi chan.
- acquire_job tich hop: limit=1 va =2 dung nhu cau hinh, khong tru 1.
- try_acquire that bai khong tru gi: sau khi that bai vi proxy day,
  release xong thi token platform/account van con nguyen.

Stdlib + pytest only. Chay tu repo root:
pytest -q tests/test_fix_scheduler_quota_proxy_per_account.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import (  # noqa: E402
    AccountStatus,
    CollectJob,
    JobStatus,
    JobType,
    Platform,
    PlatformAccount,
)
from src.scheduler.task_scheduler import (  # noqa: E402
    QuotaManager,
    TaskSchedulerImpl,
)

UTC = timezone.utc
T0 = datetime(2026, 11, 22, 10, 0, tzinfo=UTC)


def _qm(**kwargs) -> QuotaManager:
    return QuotaManager(
        per_platform_quota_per_day={Platform.TIKTOK: 1000},
        per_account_quota_per_day={Platform.TIKTOK: 1000},
        **kwargs,
    )


def _job(job_id: str, account_id: str, video_id: str) -> CollectJob:
    return CollectJob(
        id=job_id, job_type=JobType.FETCH_STATS, account_id=account_id,
        platform=Platform.TIKTOK, video_id=video_id, scheduled_at=T0,
    )


def _account(account_id: str) -> PlatformAccount:
    return PlatformAccount(
        id=account_id, workspace_id="ws-1", platform=Platform.TIKTOK,
        platform_user_id=account_id, status=AccountStatus.ACTIVE,
    )


def _scheduler(quota: QuotaManager) -> TaskSchedulerImpl:
    sched = TaskSchedulerImpl(workers=["worker-1"], quota=quota)
    for acc in ("acc-1", "acc-2", "acc-3"):
        sched.set_account_status(acc, AccountStatus.ACTIVE)
    return sched


# --- QuotaManager truc tiep ------------------------------------------------

def test_proxy_concurrency_1_runs_exactly_one_job():
    q = _qm(per_proxy_concurrency=1)
    # Loi cu: job dau tien cung bi chan (1 >= 1). Ban moi: chay duoc.
    assert q.try_acquire(account_id="acc-1", platform=Platform.TIKTOK, proxy_id="px-1") is True
    assert q.try_acquire(account_id="acc-2", platform=Platform.TIKTOK, proxy_id="px-1") is False
    q.release_proxy_slot("px-1")
    assert q.try_acquire(account_id="acc-2", platform=Platform.TIKTOK, proxy_id="px-1") is True


def test_proxy_concurrency_2_runs_exactly_two_jobs():
    q = _qm(per_proxy_concurrency=2)
    assert q.try_acquire(account_id="acc-1", platform=Platform.TIKTOK, proxy_id="px-1") is True
    assert q.try_acquire(account_id="acc-2", platform=Platform.TIKTOK, proxy_id="px-1") is True
    assert q.try_acquire(account_id="acc-3", platform=Platform.TIKTOK, proxy_id="px-1") is False
    q.release_proxy_slot("px-1")
    assert q.try_acquire(account_id="acc-3", platform=Platform.TIKTOK, proxy_id="px-1") is True
    # Proxy khac khong bi anh huong.
    assert q.try_acquire(account_id="acc-1", platform=Platform.TIKTOK, proxy_id="px-2") is True


def test_allow_is_peek_without_side_effects():
    q = _qm(per_proxy_concurrency=1)
    for _ in range(5):
        assert q.allow(account_id="acc-1", platform=Platform.TIKTOK, proxy_id="px-1") is True
    # Sau 5 lan allow, van chay duoc (allow khong tru token / khong tang proxy).
    assert q.try_acquire(account_id="acc-1", platform=Platform.TIKTOK, proxy_id="px-1") is True
    # Bay gio proxy day: allow phai False, nhung token van chua het.
    assert q.allow(account_id="acc-1", platform=Platform.TIKTOK, proxy_id="px-1") is False
    q.release_proxy_slot("px-1")
    assert q.allow(account_id="acc-1", platform=Platform.TIKTOK, proxy_id="px-1") is True


def test_per_account_quota_isolated_between_accounts():
    q = QuotaManager(
        per_platform_quota_per_day={Platform.TIKTOK: 1000},
        per_account_quota_per_day={Platform.TIKTOK: 2},
        per_proxy_concurrency=10,
    )
    assert q.try_acquire(account_id="acc-1", platform=Platform.TIKTOK, proxy_id=None) is True
    assert q.try_acquire(account_id="acc-1", platform=Platform.TIKTOK, proxy_id=None) is True
    # acc-1 het quota rieng (2/2) — loi cu: _account_buckets khong duoc
    # dung nen lan thu 3 van chay.
    assert q.try_acquire(account_id="acc-1", platform=Platform.TIKTOK, proxy_id=None) is False
    # acc-2 co bucket rieng, van chay duoc (platform moi dung 2/1000).
    assert q.try_acquire(account_id="acc-2", platform=Platform.TIKTOK, proxy_id=None) is True


def test_platform_quota_blocks_all_accounts_when_exhausted():
    q = QuotaManager(
        per_platform_quota_per_day={Platform.TIKTOK: 2},
        per_account_quota_per_day={Platform.TIKTOK: 1000},
        per_proxy_concurrency=10,
    )
    assert q.try_acquire(account_id="acc-1", platform=Platform.TIKTOK, proxy_id=None) is True
    assert q.try_acquire(account_id="acc-2", platform=Platform.TIKTOK, proxy_id=None) is True
    # Platform het (2/2): ca acc-1 lan nua va acc-3 deu bi chan.
    assert q.try_acquire(account_id="acc-1", platform=Platform.TIKTOK, proxy_id=None) is False
    assert q.try_acquire(account_id="acc-3", platform=Platform.TIKTOK, proxy_id=None) is False


def test_failed_try_acquire_consumes_nothing():
    q = _qm(per_proxy_concurrency=1)
    assert q.try_acquire(account_id="acc-1", platform=Platform.TIKTOK, proxy_id="px-1") is True
    # Job thu 2 that bai vi proxy day — khong duoc tru token platform/account.
    assert q.try_acquire(account_id="acc-2", platform=Platform.TIKTOK, proxy_id="px-1") is False
    q.release_proxy_slot("px-1")
    # Token van con: acc-2 chay duoc ngay, khong can doi refill.
    assert q.try_acquire(account_id="acc-2", platform=Platform.TIKTOK, proxy_id="px-1") is True


# --- acquire_job tich hop ----------------------------------------------------

def test_acquire_job_proxy_concurrency_1():
    sched = _scheduler(_qm(per_proxy_concurrency=1))
    sched.enqueue(_job("j1", "acc-1", "vid-1"))
    sched.enqueue(_job("j2", "acc-2", "vid-2"))
    got1 = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK, proxy_id="px-1")
    assert got1 is not None and got1.id == "j1"
    # Loi cu: job thu 2 (va ca job dau neu limit=1) bi chan vi dem 2 lan.
    assert sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK, proxy_id="px-1") is None
    assert sched.finish_job(got1, ok=True, proxy_id="px-1") == JobStatus.SUCCEEDED
    got2 = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK, proxy_id="px-1")
    assert got2 is not None and got2.id == "j2"


def test_acquire_job_proxy_concurrency_2_runs_two_concurrently():
    sched = _scheduler(_qm(per_proxy_concurrency=2))
    sched.enqueue(_job("j1", "acc-1", "vid-1"))
    sched.enqueue(_job("j2", "acc-2", "vid-2"))
    sched.enqueue(_job("j3", "acc-3", "vid-3"))
    got1 = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK, proxy_id="px-1")
    got2 = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK, proxy_id="px-1")
    assert got1 is not None and got2 is not None
    assert {got1.id, got2.id} == {"j1", "j2"}
    # Job thu 3 vuot limit=2.
    assert sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK, proxy_id="px-1") is None
    assert sched.finish_job(got1, ok=True, proxy_id="px-1") == JobStatus.SUCCEEDED
    got3 = sched.acquire_job(worker_id="worker-1", platform=Platform.TIKTOK, proxy_id="px-1")
    assert got3 is not None and got3.id == "j3"
