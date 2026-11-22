"""Tests for fix_scheduler_enqueue_fail_open (backend-sec).

enqueue() must be fail-closed: unknown account/proxy status is
REJECTED, never silently treated as ACTIVE, and never enters a queue.
Run from repo root: python -m pytest tests/test_fix_scheduler_enqueue_fail_open.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import (
    AccountStatus, CollectJob, JobStatus, JobType, Platform,
    PlatformAccount, ProxyInfo, ProxyStatus,
)
from src.scheduler.task_scheduler import TaskSchedulerImpl


def _job(account_id="acc-1", **kw):
    return CollectJob(id=f"job-{account_id}-{kw.get('video_id') or 'sync'}",
                      job_type=JobType.SYNC_VIDEOS, account_id=account_id,
                      platform=Platform.YOUTUBE, **kw)


def _total_queued(sched) -> int:
    return sum(len(q) for q in sched.queues.values())


def test_unknown_account_rejected_not_queued():
    sched = TaskSchedulerImpl()
    job = sched.enqueue(_job("ghost-account"))
    assert job.status == JobStatus.REJECTED
    assert "unknown account" in job.last_error
    assert _total_queued(sched) == 0


def test_wrong_account_id_rejected_even_when_others_known():
    sched = TaskSchedulerImpl()
    sched.set_account_status("acc-1", AccountStatus.ACTIVE)
    job = sched.enqueue(_job("acc-typo"))
    assert job.status == JobStatus.REJECTED
    assert _total_queued(sched) == 0


def test_banned_and_other_fail_closed_statuses_rejected():
    for status in (AccountStatus.BANNED, AccountStatus.CHECKPOINT,
                   AccountStatus.TOKEN_EXPIRED, AccountStatus.NEEDS_REAUTH,
                   AccountStatus.DISABLED, AccountStatus.SUSPENDED,
                   AccountStatus.CAPTCHA_REQUIRED):
        sched = TaskSchedulerImpl()
        sched.set_account_status("acc-1", status)
        job = sched.enqueue(_job())
        assert job.status == JobStatus.REJECTED, status
        assert _total_queued(sched) == 0, status


def test_known_active_account_enqueued():
    sched = TaskSchedulerImpl()
    sched.set_account_status("acc-1", AccountStatus.ACTIVE)
    job = sched.enqueue(_job())
    assert job.status == JobStatus.PENDING
    assert _total_queued(sched) == 1


def test_explicit_status_in_enqueue_is_honoured():
    sched = TaskSchedulerImpl()
    job = sched.enqueue(_job(), account_status=AccountStatus.ACTIVE)
    assert job.status == JobStatus.PENDING
    # ... and a later bare enqueue for the same account reuses the sync.
    assert sched.enqueue(_job()).status == JobStatus.PENDING


def test_register_account_and_sync_from_store():
    acc = PlatformAccount(id="acc-9", workspace_id="ws-1",
                          platform=Platform.TIKTOK, platform_user_id="u9",
                          status=AccountStatus.BANNED)
    sched = TaskSchedulerImpl()
    sched.register_account(acc)
    assert sched.enqueue(_job("acc-9")).status == JobStatus.REJECTED

    ok = PlatformAccount(id="acc-10", workspace_id="ws-1",
                         platform=Platform.TIKTOK, platform_user_id="u10",
                         status=AccountStatus.ACTIVE)
    sched2 = TaskSchedulerImpl()
    sched2.sync_from_store({"acc-10": ok})
    assert sched2.enqueue(_job("acc-10")).status == JobStatus.PENDING
    assert sched2.enqueue(_job("acc-11")).status == JobStatus.REJECTED


def test_proxy_dead_or_quarantined_rejected():
    for pstatus in (ProxyStatus.DEAD, ProxyStatus.QUARANTINED):
        sched = TaskSchedulerImpl()
        sched.set_account_status("acc-1", AccountStatus.ACTIVE)
        sched.account_proxy["acc-1"] = "proxy-1"
        sched.set_proxy_status("proxy-1", pstatus)
        job = sched.enqueue(_job())
        assert job.status == JobStatus.REJECTED, pstatus
        assert _total_queued(sched) == 0, pstatus


def test_bound_proxy_with_unknown_status_rejected():
    sched = TaskSchedulerImpl()
    acc = PlatformAccount(id="acc-1", workspace_id="ws-1",
                          platform=Platform.YOUTUBE, platform_user_id="u1",
                          status=AccountStatus.ACTIVE,
                          proxy_binding_id="proxy-unknown")
    sched.register_account(acc)  # proxy bound, but its status never synced
    job = sched.enqueue(_job())
    assert job.status == JobStatus.REJECTED
    assert "unknown proxy" in job.last_error
    assert _total_queued(sched) == 0


def test_bound_proxy_active_enqueued_via_sync_statuses():
    acc = PlatformAccount(id="acc-1", workspace_id="ws-1",
                          platform=Platform.YOUTUBE, platform_user_id="u1",
                          status=AccountStatus.ACTIVE,
                          proxy_binding_id="proxy-1")
    proxy = ProxyInfo(id="proxy-1", host="1.2.3.4", port=8080,
                      status=ProxyStatus.ACTIVE)
    sched = TaskSchedulerImpl()
    sched.sync_statuses(accounts=[acc], proxies=[proxy])
    assert sched.enqueue(_job()).status == JobStatus.PENDING


def test_proxy_id_in_payload_is_checked():
    sched = TaskSchedulerImpl()
    sched.set_account_status("acc-1", AccountStatus.ACTIVE)
    sched.set_proxy_status("proxy-9", ProxyStatus.DEAD)
    job = _job(payload={"proxy_id": "proxy-9"})
    assert sched.enqueue(job).status == JobStatus.REJECTED


def test_status_turning_banned_after_registration_blocks_new_jobs():
    sched = TaskSchedulerImpl()
    sched.set_account_status("acc-1", AccountStatus.ACTIVE)
    assert sched.enqueue(_job()).status == JobStatus.PENDING
    sched.set_account_status("acc-1", AccountStatus.BANNED)  # store sync
    job = sched.enqueue(_job(video_id="v2"))
    assert job.status == JobStatus.REJECTED
    assert _total_queued(sched) == 1  # only the first job remains


def test_requeue_dead_letter_also_fail_closed():
    sched = TaskSchedulerImpl()
    sched.set_account_status("acc-1", AccountStatus.ACTIVE)
    job = _job()
    sched.dead_letter.add(job)
    # Account gets banned while the job sits in the dead-letter queue.
    sched.set_account_status("acc-1", AccountStatus.BANNED)
    out = sched.requeue_dead_letter(job.id)
    assert out.status == JobStatus.REJECTED
    assert _total_queued(sched) == 0
