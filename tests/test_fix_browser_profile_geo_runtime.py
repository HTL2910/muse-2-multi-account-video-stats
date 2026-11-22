"""Tests for fix_browser_profile_geo_runtime (backend-sec).

Profile geo must come from the sticky proxy's ProxyInfo.country (VN ->
Asia/Ho_Chi_Minh/vi-VN, JP -> Asia/Tokyo/ja-JP), never from niche and
never defaulting to US; fingerprints stay identical across
get_or_create calls and across close/reopen cycles; close() cleans
pid/temp and all tracking state; the manual queue is durable with
status + retry.
Run from repo root: python -m pytest tests/test_fix_browser_profile_geo_runtime.py -q
"""
import dataclasses
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.browser.profile_manager import (
    IsolatedBrowserProfileManager, ProfileConflictError, SQLiteManualQueue,
)
from src.contracts import (
    AccountStatus, Platform, PlatformAccount, ProxyInfo,
)


def _account(account_id="acc-1", binding="bind-1", niche=""):
    kwargs = dict(id=account_id, platform=Platform.FACEBOOK,
                  platform_user_id="u1", status=AccountStatus.ACTIVE,
                  proxy_binding_id=binding, niche=niche)
    fields = {f.name for f in dataclasses.fields(PlatformAccount)}
    if "workspace_id" in fields:
        kwargs["workspace_id"] = "ws-1"
    return PlatformAccount(**kwargs)


def _proxy(pid, country):
    return ProxyInfo(id=pid, host="127.0.0.1", port=8080, country=country)


# ---------------------------------------------------------------- geo from sticky proxy
def test_proxy_vn_gives_vietnam_geo(tmp_path):
    mgr = IsolatedBrowserProfileManager(
        tmp_path, proxies={"bind-1": _proxy("p1", "VN")})
    prof = mgr.get_or_create(_account())
    assert (prof.timezone, prof.locale) == ("Asia/Ho_Chi_Minh", "vi-VN")
    assert mgr.geo_warnings == []


def test_proxy_jp_gives_japan_geo(tmp_path):
    mgr = IsolatedBrowserProfileManager(
        tmp_path, proxies={"bind-1": _proxy("p1", "JP")})
    prof = mgr.get_or_create(_account())
    assert (prof.timezone, prof.locale) == ("Asia/Tokyo", "ja-JP")


def test_geo_via_resolver_and_niche_is_ignored(tmp_path):
    # niche screams a country; geo must still follow the proxy (KR)
    mgr = IsolatedBrowserProfileManager(
        tmp_path, proxy_resolver=lambda binding: _proxy("p9", "KR"))
    prof = mgr.get_or_create(_account(niche="VN"))
    assert (prof.timezone, prof.locale) == ("Asia/Seoul", "ko-KR")


def test_unknown_geo_is_utc_with_warning_never_us(tmp_path):
    mgr = IsolatedBrowserProfileManager(tmp_path)  # no proxy source at all
    prof = mgr.get_or_create(_account())
    assert prof.timezone == "UTC"
    assert prof.locale == "en-US"
    assert prof.timezone != "America/New_York"  # the old silent-US bug
    assert len(mgr.geo_warnings) == 1
    assert mgr.geo_warnings[0]["account_id"] == "acc-1"


def test_geo_drift_after_creation_fails_closed(tmp_path):
    live = {"bind-1": _proxy("p1", "VN")}
    mgr = IsolatedBrowserProfileManager(
        tmp_path, proxy_resolver=lambda binding: live.get(binding))
    mgr.get_or_create(_account())
    live["bind-1"] = _proxy("p1", "JP")  # same binding, country changed
    with pytest.raises(ProfileConflictError):
        mgr.get_or_create(_account())


# ---------------------------------------------------------------- fingerprint stability
def _fp_tuple(p):
    return (p.fingerprint_key, p.user_agent, p.timezone, p.locale,
            p.viewport_width, p.viewport_height, p.user_data_dir)


def test_fingerprint_stable_across_get_or_create_and_reopen(tmp_path):
    mgr = IsolatedBrowserProfileManager(
        tmp_path, proxies={"bind-1": _proxy("p1", "VN")})
    acc = _account()
    p1 = mgr.get_or_create(acc)
    p2 = mgr.get_or_create(acc)
    assert _fp_tuple(p1) == _fp_tuple(p2)
    mgr.close(p1.id)
    p3 = mgr.get_or_create(acc)  # recreated deterministically from seed
    assert _fp_tuple(p3) == _fp_tuple(p1)
    assert mgr.validate_owner(p3, acc.id)


# ---------------------------------------------------------------- close / runtime lifecycle
def test_close_removes_all_tracking_and_cleans_temp(tmp_path):
    mgr = IsolatedBrowserProfileManager(
        tmp_path, proxies={"bind-1": _proxy("p1", "VN")})
    prof = mgr.get_or_create(_account())
    scratch = tmp_path / "scratch-temp"
    scratch.mkdir()
    (scratch / "junk.bin").write_text("x")
    profile_tmp = Path(prof.user_data_dir) / "tmp"
    profile_tmp.mkdir()
    (profile_tmp / "junk2.bin").write_text("y")
    mgr.register_runtime(prof.id, temp_dir=str(scratch))

    mgr.close(prof.id)
    assert not scratch.exists(), "runtime temp dirs must be removed"
    assert not profile_tmp.exists(), "profile tmp dir must be removed"
    assert mgr.runtime_for(prof.id) is None
    assert prof.id not in mgr._profiles and "acc-1" not in mgr._by_account
    mgr.close(prof.id)  # idempotent: closing again is a no-op


def test_close_kills_registered_process(tmp_path):
    mgr = IsolatedBrowserProfileManager(
        tmp_path, proxies={"bind-1": _proxy("p1", "VN")})
    prof = mgr.get_or_create(_account())
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            start_new_session=True)
    try:
        mgr.register_runtime(prof.id, pid=proc.pid)
        mgr.close(prof.id)
        deadline = time.time() + 5
        while proc.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert proc.poll() is not None, "close() must terminate the browser pid"
    finally:
        if proc.poll() is None:
            proc.kill()


def test_close_reopen_cycles_stay_clean(tmp_path):
    mgr = IsolatedBrowserProfileManager(
        tmp_path, proxies={"bind-1": _proxy("p1", "JP")})
    acc = _account()
    expected = None
    for cycle in range(5):
        prof = mgr.get_or_create(acc)
        if expected is None:
            expected = _fp_tuple(prof)
        assert _fp_tuple(prof) == expected
        assert mgr.runtime_for(prof.id) is not None
        scratch = tmp_path / f"cycle-{cycle}"
        scratch.mkdir()
        mgr.register_runtime(prof.id, temp_dir=str(scratch))
        mgr.close(prof.id)
        assert not scratch.exists()
        assert mgr.runtime_for(prof.id) is None
        assert not mgr._profiles and not mgr._by_account and not mgr._runtime


def test_existing_profile_recreates_missing_runtime(tmp_path):
    mgr = IsolatedBrowserProfileManager(
        tmp_path, proxies={"bind-1": _proxy("p1", "VN")})
    acc = _account()
    prof = mgr.get_or_create(acc)
    mgr._runtime.pop(prof.id)  # simulate the old close() bug's aftermath
    again = mgr.get_or_create(acc)
    assert again.id == prof.id
    assert mgr.runtime_for(prof.id) is not None, "existing branch must rebuild _runtime"


# ---------------------------------------------------------------- durable manual queue
def test_manual_queue_survives_restart_with_status_and_retry(tmp_path):
    mgr1 = IsolatedBrowserProfileManager(tmp_path)
    acc = _account()
    status = mgr1.handle_block(acc, url="https://x/captcha", text="are you a robot")
    from src.contracts import AccountStatus as AS
    assert status == AS.CAPTCHA_REQUIRED
    items = mgr1.manual_queue
    assert len(items) == 1 and items[0]["status"] == "PENDING"
    item_id = items[0]["id"]

    # duplicate block for the same account+kind does not double-queue
    mgr1.handle_block(acc, url="https://x/captcha", text="captcha again")
    assert len(mgr1.manual_queue) == 1

    # "restart": a brand-new manager on the same base_dir sees the item
    mgr2 = IsolatedBrowserProfileManager(tmp_path)
    items2 = mgr2.manual_queue
    assert len(items2) == 1 and items2[0]["id"] == item_id

    claimed = mgr2.claim_manual_item(item_id)
    assert claimed["status"] == "IN_PROGRESS" and claimed["attempts"] == 1
    assert mgr2.claim_manual_item(item_id) is None  # cannot claim twice
    # fail under the retry budget -> back to PENDING; then complete it
    assert mgr2.fail_manual_item(item_id, "operator busy") == "PENDING"
    claimed = mgr2.claim_manual_item(item_id)
    assert claimed["attempts"] == 2
    assert mgr2.complete_manual_item(item_id) is True
    assert mgr2.manual_queue == []


def test_manual_queue_fail_exhausts_to_failed(tmp_path):
    q = SQLiteManualQueue(tmp_path / "mq.sqlite3")
    item_id = q.enqueue({"account_id": "a1", "kind": "checkpoint", "url": "u"})
    for _ in range(3):
        q.claim(item_id)
        state = q.fail(item_id, "boom", max_attempts=3)
    assert state == "FAILED"
    assert q.get(item_id)["status"] == "FAILED"
    assert q.list_items() == []  # FAILED is out of the pending view
