"""fix_monitor_golden_killswitch_prometheus — unwrap, kill-switch, TYPE.

- GoldenRunner cu: items=raw.get('data',[]) nen fixture TikTok data la
  dict {'videos': [...]} cho len=3 (so key) va items[0] KeyError.
- KillSwitch cu: nguong mac dinh 0.9 qua muon, khong noi nao kiem tra
  tripped nen la dead code.
- prometheus_text() cu: in '# TYPE' truoc moi dong label.

Test tu dua repo root len sys.path de khong phu thuoc conftest/pyproject.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.contracts import (
    GoldenCase,
    JobStatus,
    Platform,
    UnifiedVideoMetric,
)
from src.observability.monitor import (
    KILL_SWITCH_ERROR_RATE_THRESHOLD,
    GoldenRunner,
    KillSwitch,
    Monitor,
    kill_switch_gate,
    unwrap_fixture_items,
)

TIKTOK_FIXTURE = {
    "data": {
        "videos": [
            {"id": "tt-1", "views": 100, "likes": 5, "comments": 2, "shares": 1},
            {"id": "tt-2", "views": 200, "likes": 9, "comments": 3, "shares": 2},
        ],
        "cursor": "abc",
        "has_more": False,
    }
}


def _load_json(path: str):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _map_tiktok(platform, raw):
    return UnifiedVideoMetric(
        video_id=f"vid-{raw['id']}", account_id="acc-1", platform=platform,
        platform_video_id=raw["id"], views=raw["views"], likes=raw["likes"],
        comments=raw["comments"], shares=raw["shares"],
        date_bucket="2026-11-22",
    )


def _expected_first() -> UnifiedVideoMetric:
    return UnifiedVideoMetric(
        video_id="vid-tt-1", account_id="acc-1", platform=Platform.TIKTOK,
        platform_video_id="tt-1", views=100, likes=5, comments=2, shares=1,
        date_bucket="2026-11-22",
    )


def test_unwrap_fixture_items_all_shapes():
    assert unwrap_fixture_items([{"id": "a"}, {"id": "b"}]) == [{"id": "a"}, {"id": "b"}]
    assert unwrap_fixture_items({"data": [{"id": "a"}]}) == [{"id": "a"}]
    assert unwrap_fixture_items(TIKTOK_FIXTURE) == TIKTOK_FIXTURE["data"]["videos"]
    assert unwrap_fixture_items({"videos": [{"id": "a"}]}) == [{"id": "a"}]
    assert unwrap_fixture_items({"data": {"data": [{"id": "a"}]}}) == [{"id": "a"}]
    assert unwrap_fixture_items({"data": "not-a-list"}) == []
    assert unwrap_fixture_items(None) == []
    assert unwrap_fixture_items("x") == []


def test_golden_runner_with_real_tiktok_fixture_shape(tmp_path):
    fixture = tmp_path / "video_list.json"
    fixture.write_text(json.dumps(TIKTOK_FIXTURE), encoding="utf-8")
    runner = GoldenRunner(load_fixture=_load_json, adapter_map=_map_tiktok)
    ok, errors = runner.run(GoldenCase(
        id="golden-tiktok-1", name="tiktok video list",
        platform=Platform.TIKTOK, raw_fixture_path=str(fixture),
        expected_video_count=2, expected_metric=_expected_first(),
    ))
    assert ok, errors
    # Count sai phai fail, khong phai KeyError gia vo mapping.
    ok2, errors2 = runner.run(GoldenCase(
        id="golden-tiktok-1", name="tiktok video list",
        platform=Platform.TIKTOK, raw_fixture_path=str(fixture),
        expected_video_count=3, expected_metric=_expected_first(),
    ))
    assert not ok2 and any("video count 2 != expected 3" in e for e in errors2)


def test_golden_runner_with_repo_tiktok_fixture_if_present():
    path = REPO_ROOT / "data" / "golden" / "tiktok" / "video_list.json"
    if not path.is_file():
        pytest.skip("repo chua co data/golden/tiktok/video_list.json o ban sao nay")
    raw = _load_json(str(path))
    items = unwrap_fixture_items(raw)
    assert isinstance(items, list) and len(items) > 0
    assert all(isinstance(v, dict) and v.get("id") for v in items)


def test_kill_switch_default_threshold_is_not_0_9():
    assert KILL_SWITCH_ERROR_RATE_THRESHOLD == 0.4
    assert 0.3 <= KILL_SWITCH_ERROR_RATE_THRESHOLD <= 0.5
    ks = KillSwitch()
    assert ks.threshold == 0.4


def test_kill_switch_trips_at_40_percent_errors():
    ks = KillSwitch(min_samples=20)
    for _ in range(10):
        assert not ks.record(True)
    for _ in range(10):
        ks.record(False)
    assert ks.tripped
    assert ks.error_rate == pytest.approx(0.5)
    assert ks.tripped_at is not None


def test_kill_switch_does_not_trip_below_threshold():
    ks = KillSwitch(min_samples=20)
    for _ in range(16):
        ks.record(True)
    for _ in range(4):
        assert not ks.record(False)
    assert not ks.tripped


def test_kill_switch_gate_rejects_when_tripped_and_allows_otherwise():
    ks = KillSwitch(min_samples=20)
    assert kill_switch_gate(ks) == (True, "")
    for _ in range(10):
        ks.record(True)
    for _ in range(10):
        ks.record(False)
    allowed, reason = kill_switch_gate(ks)
    assert not allowed
    assert "kill-switch tripped" in reason
    assert "0.50" in reason


def test_monitor_record_job_feeds_kill_switch_and_gate():
    monitor = Monitor(kill_switch_threshold=0.4)
    assert monitor.check_kill_switch() == (True, "")
    for _ in range(12):
        monitor.record_job(ok=True, platform=Platform.FACEBOOK, job_status=JobStatus.SUCCEEDED)
    for _ in range(8):
        monitor.record_job(ok=False, platform=Platform.FACEBOOK, job_status=JobStatus.FAILED)
    assert monitor.kill_switch.tripped
    assert monitor.error_rate == pytest.approx(0.4)
    allowed, _ = monitor.check_kill_switch()
    assert not allowed
    # Day la diem noi vao scheduler: enqueue()/acquire_job() phai goi
    # check_kill_switch() / kill_switch_gate() va REJECT khi (False, ...).
    monitor.kill_switch.reset()
    assert monitor.check_kill_switch() == (True, "")


def test_prometheus_type_printed_once_per_metric_name():
    monitor = Monitor()
    monitor.inc("jobs_total", labels={"platform": "facebook", "status": "succeeded"})
    monitor.inc("jobs_total", labels={"platform": "tiktok", "status": "failed"})
    monitor.inc("jobs_total", labels={"platform": "facebook", "status": "failed"})
    monitor.inc("alerts_total", labels={"type": "sync_failed", "severity": "warning"})
    monitor.inc("alerts_total", labels={"type": "sync_failed", "severity": "critical"})
    monitor.set_gauge("accounts_active", 3.0)
    text = monitor.prometheus_text()
    assert text.count("# TYPE jobs_total counter") == 1
    assert text.count("# TYPE alerts_total counter") == 1
    assert text.count("# TYPE accounts_active gauge") == 1
    assert text.count("jobs_total{") == 3
    assert text.count("alerts_total{") == 2
    assert "accounts_active 3.0" in text
    # TYPE phai dung truoc dong du lieu cua no, khong chen giua cac dong.
    assert text.index("# TYPE jobs_total counter") < text.index('jobs_total{platform="facebook"')


def test_prometheus_empty_is_empty():
    assert Monitor().prometheus_text() == ""
