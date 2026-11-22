"""fix_unified_store_pipeline_wiring — mot store, gate fail-closed, noi store.

- pipeline.run() cu goi thang adapter ma khong kiem tra FAIL_CLOSED /
  proxy nen account CHECKPOINT/BANNED van bi crawl.
- Ba InMemoryMetricsStore roi rac + store rieng cua API: sync xong
  dashboard van rong.
- src/registry.py chi co APPROACH_REGISTRY={} trong, khong ai import.

Test dung fake adapter (khong goi API that). Tu dua repo root len
sys.path de khong phu thuoc conftest/pyproject.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.contracts import (
    AccountStatus,
    BatchDecision,
    JobStatus,
    Platform,
    PlatformAccount,
    ProxyInfo,
    ProxyStatus,
    UnifiedVideoMetric,
    Video,
    VideoStatsSnapshot,
    VideoStatus,
)
from src.core.models import InMemoryMetricsStore as CoreMetricsStore
from src.ingestion.pipeline import (
    IngestionPipeline,
    InMemoryMetricsStore,
    get_shared_store,
)

TS = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)


def _account(status=AccountStatus.ACTIVE) -> PlatformAccount:
    return PlatformAccount(
        id="acc-1", workspace_id="ws-1", platform=Platform.FACEBOOK,
        platform_user_id="page-1", status=status,
        credential_ref="vault://facebook/acc-1",
    )


def _video(pid="v1") -> Video:
    return Video(id=f"vid-{pid}", account_id="acc-1", platform=Platform.FACEBOOK,
                 platform_video_id=pid, title="T", status=VideoStatus.ACTIVE)


def _metric(video: Video, views: int = 100) -> UnifiedVideoMetric:
    return UnifiedVideoMetric(
        video_id=video.id, account_id="acc-1", platform=Platform.FACEBOOK,
        platform_video_id=video.platform_video_id, views=views, likes=5,
        comments=2, shares=1, watch_time_seconds=60,
        date_bucket="2026-11-22", collected_at=TS,
    )


class FakeAdapter:
    def __init__(self, videos=None):
        self.videos = videos if videos is not None else [_video("v1"), _video("v2")]
        self.list_calls = 0
        self.stats_calls = 0

    def list_videos(self, account, *, limit=50):
        self.list_calls += 1
        return self.videos[:limit]

    def get_video_stats(self, account, video):
        self.stats_calls += 1
        return _metric(video)


def test_pipeline_store_delegates_to_canonical_core_store():
    assert issubclass(InMemoryMetricsStore, CoreMetricsStore)
    store = InMemoryMetricsStore()
    snap = store.save_snapshot(_metric(_video("v1")))
    assert isinstance(snap, VideoStatsSnapshot)
    assert snap.metric.views == 100
    assert len(store) == 1


def test_shared_store_is_single_instance():
    assert get_shared_store() is get_shared_store()
    store = get_shared_store()
    # Tam xoa du lieu cu (neu test khac da dung store chung) bang cach
    # kiem tra tinh chat: pipeline viet vao store chung thi store chung thay.
    pipeline = IngestionPipeline(FakeAdapter([_video("shared-1")]), store)
    report = pipeline.run(_account(), job_id="j-shared")
    assert report.metrics_written >= 1 or report.duplicates_skipped >= 1
    assert store.get_latest(video_id="vid-shared-1") is not None
    # Pipeline thu 2 doc cung store thay snapshot cua pipeline 1.
    assert IngestionPipeline(FakeAdapter([]), store).store is store


@pytest.mark.parametrize("status", [
    AccountStatus.CHECKPOINT, AccountStatus.BANNED, AccountStatus.TOKEN_EXPIRED,
    AccountStatus.NEEDS_REAUTH, AccountStatus.SUSPENDED,
])
def test_fail_closed_account_is_rejected_without_calling_adapter(status):
    adapter = FakeAdapter()
    pipeline = IngestionPipeline(adapter, InMemoryMetricsStore())
    report = pipeline.run(_account(status), job_id="j-reject")
    assert report.decision is BatchDecision.REJECT
    assert report.metrics_written == 0 and report.videos_found == 0
    assert adapter.list_calls == 0 and adapter.stats_calls == 0
    result = report.to_job_result("j-reject")
    assert result.status is JobStatus.REJECTED
    assert result.decision is BatchDecision.REJECT
    assert "fail-closed" in result.error_code


def test_inactive_proxy_is_rejected_without_calling_adapter():
    adapter = FakeAdapter()
    pipeline = IngestionPipeline(adapter, InMemoryMetricsStore())
    proxy = ProxyInfo(id="px-1", host="1.2.3.4", port=8080, status=ProxyStatus.QUARANTINED)
    report = pipeline.run(_account(), job_id="j-px", proxy=proxy)
    assert report.decision is BatchDecision.REJECT
    assert adapter.list_calls == 0
    assert report.to_job_result("j-px").status is JobStatus.REJECTED


def test_proxy_resolver_none_means_no_proxy_bound_rejected():
    adapter = FakeAdapter()
    pipeline = IngestionPipeline(adapter, InMemoryMetricsStore(),
                                 proxy_resolver=lambda a: None)
    report = pipeline.run(_account(), job_id="j-nopx")
    assert report.decision is BatchDecision.REJECT
    assert adapter.list_calls == 0


def test_active_proxy_and_active_account_run_normally():
    adapter = FakeAdapter()
    pipeline = IngestionPipeline(adapter, InMemoryMetricsStore())
    proxy = ProxyInfo(id="px-1", host="1.2.3.4", port=8080, status=ProxyStatus.ACTIVE)
    report = pipeline.run(_account(), job_id="j-ok", proxy=proxy)
    assert report.decision is BatchDecision.ACCEPT
    assert report.videos_found == 2 and report.metrics_written == 2
    assert adapter.list_calls == 1 and adapter.stats_calls == 2
    assert report.to_job_result("j-ok").status is JobStatus.SUCCEEDED


def test_no_module_imports_dead_registry():
    """src/registry.py chi co mot dict trong; khong file src nao duoc
    phep import no — xoa file la git rm o buoc merge."""
    hits = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*(from\s+(src\.)?registry\s+import|import\s+registry)\b", text, re.M):
            hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == [], hits
