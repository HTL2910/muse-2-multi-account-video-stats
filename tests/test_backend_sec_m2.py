"""test_backend_sec_m2 — coverage for the secret-bearing / fail-closed
modules that the old suite never imported:

  * src.accounts.vault          — store/rotate/restart/tamper, KEK
                                    fail-closed, distributed-lock token
                                    refresh race, revoked -> NEEDS_REAUTH
  * src.connectors.youtube_connector — quota fail-closed, <=50-id
                                    batching, Data/Analytics mapping
  * src.browser.profile_manager — fingerprint stability, proxy mismatch
                                    fail-closed, checkpoint -> manual queue
  * src.api.alert_rule_engine   — dedupe/cooldown, sync-fail x3, spike/drop
  * scripts/collect_sample_data — golden cases for all 3 platforms and
                                    no real credentials in fixtures

Portable: repo root is derived from this file's location, never a
hard-coded absolute path. Plain asserts so it runs under pytest and
directly (`python tests/test_backend_sec_m2.py`).
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import tempfile
import threading
import time
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.contracts import (  # noqa: E402
    AccountStatus, Platform, PlatformAccount, TokenSet,
    UnifiedVideoMetric, Video, VideoStatus, utcnow,
)
from src.accounts import vault as vault_mod  # noqa: E402
from src.accounts.vault import (  # noqa: E402
    EncryptedSecretVault, EnvKekProvider, InMemoryDistributedLock,
    InMemoryVaultStore, TokenManager, TokenRevokedError, VaultError,
)
from src.connectors.youtube_connector import (  # noqa: E402
    QuotaExceededError, QuotaTracker, YouTubeAuthError, YouTubeConnector,
    parse_duration_iso8601,
)
from src.browser.profile_manager import (  # noqa: E402
    IsolatedBrowserProfileManager, ProfileConflictError, detect_block_page,
)
from src.api.alert_rule_engine import (  # noqa: E402
    AlertRuleEngine, AlertType, MultiChannelNotifier, NotificationCenter,
)


def _load_collect():
    spec = importlib.util.spec_from_file_location(
        "collect_sample_data", REPO_ROOT / "scripts" / "collect_sample_data.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_KEK = bytes(range(32))          # fixed test KEK (not a real secret)
_KEK_OTHER = bytes(range(32, 64))


def _make_vault(store=None):
    """Vault that works with or without the `cryptography` package:
    dev fallback is explicitly opted into for tests only."""
    return EncryptedSecretVault(
        kek=EnvKekProvider(kek=_KEK, dev_mode=True),
        store=store if store is not None else InMemoryVaultStore(),
        allow_insecure_fallback=True)


def _account(**kw) -> PlatformAccount:
    base = dict(id="acc1", workspace_id="ws1", platform=Platform.YOUTUBE,
                platform_user_id="UC_test_channel", display_name="Acc One",
                status=AccountStatus.ACTIVE)
    base.update(kw)
    return PlatformAccount(**base)


def _expect(exc_type, fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except exc_type:
        return
    raise AssertionError(f"expected {exc_type.__name__} from {fn!r}")


# ------------------------------------------------------------------ vault
def test_vault_store_retrieve_roundtrip_and_no_plaintext_at_rest():
    v = _make_vault()
    ref = v.store(account_id="acc1", kind="access_token",
                  secret="super-secret-token-value")
    assert ref.startswith("vault://acc1/access_token/v1/")
    assert v.retrieve(credential_ref=ref) == "super-secret-token-value"
    rec = v.record_for(ref)
    assert rec is not None
    # Ciphertext at rest must never contain the plaintext secret.
    assert "super-secret-token-value" not in rec.ciphertext
    assert "super-secret-token-value" not in rec.wrapped_dek


def test_vault_rotate_invalidates_old_ref():
    v = _make_vault()
    ref1 = v.store(account_id="acc1", kind="password", secret="old-pass")
    ref2 = v.rotate(credential_ref=ref1, new_secret="new-pass")
    assert ref2 != ref1
    assert v.retrieve(credential_ref=ref2) == "new-pass"
    _expect(VaultError, v.retrieve, credential_ref=ref1)  # old destroyed
    rec2 = v.record_for(ref2)
    assert rec2 is not None and rec2.rotated_from == ref1


def test_vault_restart_with_same_kek_recovers_and_wrong_kek_fails():
    store = InMemoryVaultStore()
    v1 = _make_vault(store)
    ref = v1.store(account_id="acc9", kind="cookie", secret="session=abc")
    # "Restart": brand-new provider + vault over the SAME store & KEK.
    v2 = _make_vault(store)
    assert v2.retrieve(credential_ref=ref) == "session=abc"
    # Restart with a DIFFERENT KEK must fail closed, not return garbage.
    v3 = EncryptedSecretVault(
        kek=EnvKekProvider(kek=_KEK_OTHER, dev_mode=True), store=store,
        allow_insecure_fallback=True)
    _expect(VaultError, v3.retrieve, credential_ref=ref)


def test_vault_tampered_ciphertext_is_detected():
    v = _make_vault()
    ref = v.store(account_id="acc1", kind="session", secret="payload-123")
    rec = v.record_for(ref)
    assert rec is not None
    flipped = ("A" if not rec.ciphertext.startswith("A") else "B") \
        + rec.ciphertext[1:]
    rec.ciphertext = flipped  # simulate storage tampering / bit rot
    _expect(VaultError, v.retrieve, credential_ref=ref)


def test_vault_missing_kek_fails_closed_outside_dev_mode():
    saved = {k: os.environ.pop(k, None)
             for k in ("VAULT_KEK_B64", "VAULT_DEV_MODE", "DEV_MODE")}
    try:
        _expect(VaultError, EnvKekProvider, dev_mode=False)
        # An invalid / short KEK from env is also rejected, never truncated.
        os.environ["VAULT_KEK_B64"] = "not-valid-base64!!"
        _expect(VaultError, EnvKekProvider, dev_mode=False)
    finally:
        for k, val in saved.items():
            if val is not None:
                os.environ[k] = val
        os.environ.pop("VAULT_KEK_B64", None)


def test_distributed_lock_owner_semantics():
    lock = InMemoryDistributedLock()
    owner = lock.acquire("k1", ttl_seconds=30, timeout_seconds=1)
    assert owner
    # Second contender times out -> None (fail-closed), never steals.
    assert lock.acquire("k1", ttl_seconds=30, timeout_seconds=0.2) is None
    # A non-owner release must NOT delete someone else's lock.
    assert lock.release("k1", "not-the-owner") is False
    assert lock.acquire("k1", ttl_seconds=30, timeout_seconds=0.2) is None
    assert lock.release("k1", owner) is True
    assert lock.acquire("k1", ttl_seconds=30, timeout_seconds=0.5)


def _refresh_method(tm):
    """Find TokenManager's public refresh entry point without assuming
    one exact name (the manager's contract is: given an account, return
    a fresh TokenSet)."""
    candidates = ["ensure_fresh", "ensure_fresh_token", "ensure_valid_token",
                  "get_valid_token", "refresh_if_needed", "get_fresh_token",
                  "refresh_token_if_needed", "refresh"]
    for name in candidates:
        fn = getattr(tm, name, None)
        if callable(fn):
            return fn
    for name in dir(tm):
        if not name.startswith("_") and ("refresh" in name or "fresh" in name):
            fn = getattr(tm, name)
            if callable(fn):
                return fn
    raise AssertionError(
        f"TokenManager exposes no public refresh method: {dir(tm)}")


def _call_refresh(tm, account, vault):
    fn = _refresh_method(tm)
    params = [p for p in inspect.signature(fn).parameters.values()
              if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    if len(params) >= 2:
        tokens = vault.retrieve_token_set(credential_ref=account.credential_ref)
        result = fn(account, tokens)
    else:
        result = fn(account)
    if isinstance(result, tuple):  # tolerate (TokenSet, status) shapes
        result = result[0]
    return result


def test_token_refresh_race_calls_refresher_exactly_once():
    vault = _make_vault()
    old = TokenSet(access_token="access-old", refresh_token="refresh-old",
                   expires_at=utcnow() + timedelta(minutes=5))  # expiring
    ref = vault.store_token_set(account_id="acc-race", tokens=old)
    calls = {"n": 0}
    calls_lock = threading.Lock()

    def refresher(account, tokens):
        with calls_lock:
            calls["n"] += 1
        time.sleep(0.05)  # widen the race window
        return TokenSet(access_token="access-new",
                        refresh_token="refresh-new",
                        expires_at=utcnow() + timedelta(hours=2))

    shared_lock = InMemoryDistributedLock()
    results, errors = [], []

    def worker():
        try:
            acct = _account(id="acc-race", credential_ref=ref)
            tm = TokenManager(vault, refresher, lock_provider=shared_lock)
            results.append(_call_refresh(tm, acct, vault))
        except Exception as exc:  # noqa: BLE001 - collected & asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors, f"refresh workers errored: {errors!r}"
    assert len(results) == 6
    # The distributed lock + latest-ref re-read means ONE real refresh;
    # every worker ends up with the winner's rotated token.
    assert calls["n"] == 1, f"refresher called {calls['n']} times"
    assert all(r.access_token == "access-new" for r in results)
    latest = vault.latest_ref("acc-race", "token_set")
    assert latest is not None and latest != ref


def test_token_revoked_fails_closed_to_needs_reauth():
    vault = _make_vault()
    old = TokenSet(access_token="access-old", refresh_token="refresh-old",
                   expires_at=utcnow() + timedelta(minutes=5))
    ref = vault.store_token_set(account_id="acc-revoked", tokens=old)
    acct = _account(id="acc-revoked", credential_ref=ref)

    def revoked_refresher(account, tokens):
        raise TokenRevokedError("refresh token revoked by platform")

    tm = TokenManager(vault, revoked_refresher,
                      lock_provider=InMemoryDistributedLock())
    raised = False
    try:
        _call_refresh(tm, acct, vault)
    except TokenRevokedError:
        raised = True
    # Fail-closed contract (vault module docstring): revoked/checkpoint
    # -> NEEDS_REAUTH, surfaced as TokenRevokedError and/or the status.
    assert raised or acct.status == AccountStatus.NEEDS_REAUTH, (
        f"revoked token neither raised nor set NEEDS_REAUTH "
        f"(status={acct.status})")


# ---------------------------------------------------------------- youtube
class _FakeHTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status = status


def _yt_transport(calls, *, uploads_count=0, analytics_rows=None):
    def transport(method, params):
        calls.append((method, dict(params)))
        if method == "channels.list" and params.get("part") == "contentDetails":
            return {"items": [{"contentDetails":
                               {"relatedPlaylists": {"uploads": "UU_test"}}}]}
        if method == "channels.list":
            return {"items": [{"id": "UC_test_channel"}]}
        if method == "playlistItems.list":
            return {"items": [{"contentDetails": {"videoId": f"vid{i:03d}"}}
                              for i in range(uploads_count)]}
        if method == "videos.list" and params.get("part") == "statistics":
            return {"items": [{"id": params["id"], "statistics": {
                "viewCount": "12345", "likeCount": "678", "commentCount": "90"}}]}
        if method == "videos.list":
            ids = params["id"].split(",")
            return {"items": [{
                "id": vid,
                "snippet": {"title": f"Video {vid}",
                            "publishedAt": "2026-01-01T00:00:00Z",
                            "thumbnails": {"high": {"url": "https://x.invalid/t.jpg"}}},
                "contentDetails": {"duration": "PT1H2M3S"},
                "statistics": {"viewCount": "10"},
                "status": {"privacyStatus": "public"}} for vid in ids]}
        if method == "analytics.reports.query":
            return {"rows": analytics_rows or []}
        raise AssertionError(f"unexpected method {method}")
    return transport


def test_youtube_quota_fail_closed_before_any_transport_call():
    tracker = QuotaTracker(daily_limit=150)
    tracker.charge("search.list")            # 100 units
    assert tracker.remaining == 50
    _expect(QuotaExceededError, tracker.charge, "search.list")
    calls = []
    conn = YouTubeConnector(_yt_transport(calls),
                            quota=QuotaTracker(daily_limit=50))
    _expect(QuotaExceededError, conn._call, "search.list", {"q": "x"})
    assert calls == [], "quota-exceeded call must never reach transport"


def test_youtube_list_videos_batches_at_most_50_ids():
    calls = []
    conn = YouTubeConnector(_yt_transport(calls, uploads_count=120))
    videos = conn.list_videos(_account(), limit=120)
    assert len(videos) == 120
    batch_sizes = [len(p["id"].split(",")) for m, p in calls
                   if m == "videos.list"]
    assert batch_sizes == [50, 50, 20], batch_sizes
    assert videos[0].duration_seconds == 3723  # PT1H2M3S
    assert videos[0].status == VideoStatus.ACTIVE
    assert parse_duration_iso8601("PT3M20S") == 200
    assert parse_duration_iso8601("bogus") is None


def test_youtube_stats_and_analytics_mapping():
    rows = [[10, 1000, 5.0, 7, 2, 999]]  # minutes, impr, ctr%, +subs, -subs, views
    conn = YouTubeConnector(_yt_transport([], analytics_rows=rows))
    acct = _account()
    video = Video(id="v1", account_id=acct.id, platform=Platform.YOUTUBE,
                  platform_video_id="vid000")
    metric = conn.get_video_stats(acct, video)
    assert (metric.views, metric.likes, metric.comments) == (12345, 678, 90)
    full = conn.get_video_stats_full(acct, video, start_date="2026-01-01",
                                     end_date="2026-01-31")
    assert full.watch_time_seconds == 600       # 10 minutes -> seconds
    assert full.impressions == 1000
    assert full.ctr == 0.05                     # percent -> fraction
    assert full.followers_delta == 5            # 7 gained - 2 lost
    assert full.views == 12345                  # Data API wins for views


def test_youtube_auth_error_maps_to_check_alive_false():
    def dead_transport(method, params):
        raise _FakeHTTPError(401)
    conn = YouTubeConnector(dead_transport)
    assert conn.check_alive(_account()) is False
    _expect(YouTubeAuthError, conn.authenticate, _account())  # no provider


# --------------------------------------------------------- profile manager
def test_profile_fingerprint_is_stable_across_instances():
    with tempfile.TemporaryDirectory() as td:
        acct = _account(id="acc-fp", proxy_binding_id="proxy-1")
        p1 = IsolatedBrowserProfileManager(td).get_or_create(acct)
        p2 = IsolatedBrowserProfileManager(td).get_or_create(acct)
        assert p1.user_agent == p2.user_agent
        assert (p1.viewport_width, p1.viewport_height) == \
               (p2.viewport_width, p2.viewport_height)
        assert p1.fingerprint_key == p2.fingerprint_key
        other = IsolatedBrowserProfileManager(td).get_or_create(
            _account(id="acc-other", proxy_binding_id="proxy-1"))
        assert other.fingerprint_key != p1.fingerprint_key
        assert other.user_data_dir != p1.user_data_dir


def test_profile_proxy_mismatch_fails_closed():
    with tempfile.TemporaryDirectory() as td:
        mgr = IsolatedBrowserProfileManager(td)
        acct = _account(id="acc-px", proxy_binding_id="proxy-A")
        mgr.get_or_create(acct)
        acct.proxy_binding_id = "proxy-B"  # sticky proxy changed under it
        _expect(ProfileConflictError, mgr.get_or_create, acct)
        # A profile with NO proxy bound must refuse to launch at all.
        bare = _account(id="acc-noproxy", proxy_binding_id=None)
        prof = mgr.get_or_create(bare)
        _expect(ProfileConflictError, mgr.launch_args, prof)


def test_profile_checkpoint_goes_to_manual_queue():
    with tempfile.TemporaryDirectory() as td:
        mgr = IsolatedBrowserProfileManager(td)
        acct = _account(id="acc-block")
        status = mgr.handle_block(acct, text="Please complete the captcha")
        assert status == AccountStatus.CAPTCHA_REQUIRED
        status = mgr.handle_block(acct, url="https://x.invalid/checkpoint")
        assert status == AccountStatus.CHECKPOINT
        assert len(mgr.manual_queue) == 2
        assert all(e["action"] == "manual_review" for e in mgr.manual_queue)
        # Clean page: no status change, nothing queued.
        assert mgr.handle_block(acct, text="Welcome back") is None
        assert len(mgr.manual_queue) == 2
        assert detect_block_page(text="are you a robot?") == "captcha"
        prof = mgr.get_or_create(_account(id="acc-own", proxy_binding_id="p"))
        assert mgr.validate_owner(prof, "acc-own") is True
        assert mgr.validate_owner(prof, "someone-else") is False


# ------------------------------------------------------------ alert engine
def _metric(video_id, views):
    return UnifiedVideoMetric(video_id=video_id, account_id="acc1",
                              platform=Platform.YOUTUBE,
                              platform_video_id=f"pv-{video_id}", views=views)


def test_alert_dedupe_within_cooldown():
    center = NotificationCenter()
    engine = AlertRuleEngine(center)
    first = engine.on_proxy_dead(workspace_id="ws1", proxy_id="px1")
    second = engine.on_proxy_dead(workspace_id="ws1", proxy_id="px1")
    assert first is not None and first.alert_type == AlertType.PROXY_DEAD
    assert second is None                      # deduped by cooldown
    assert len(center.unread("ws1")) == 1


def test_alert_sync_fail_fires_on_third_consecutive_only():
    center = NotificationCenter()
    engine = AlertRuleEngine(center)
    acct = _account()
    assert engine.on_sync_result(acct, ok=False) is None   # 1st
    assert engine.on_sync_result(acct, ok=False) is None   # 2nd
    third = engine.on_sync_result(acct, ok=False)          # 3rd -> alert
    assert third is not None and third.alert_type == AlertType.SYNC_FAILED
    # A success resets the counter: two fresh failures stay silent.
    assert engine.on_sync_result(acct, ok=True) is None
    assert engine.on_sync_result(acct, ok=False) is None
    assert engine.on_sync_result(acct, ok=False) is None
    assert len(center.alerts) == 1


def test_alert_spike_and_drop_rules():
    center = NotificationCenter()
    engine = AlertRuleEngine(center)
    spike = engine.on_metric(workspace_id="ws1", account_id="acc1",
                             current=_metric("v-spike", 5000),
                             previous=_metric("v-spike", 1000))
    assert [a.alert_type for a in spike] == [AlertType.VIDEO_SPIKE]
    drop = engine.on_metric(workspace_id="ws1", account_id="acc1",
                            current=_metric("v-drop", 400),
                            previous=_metric("v-drop", 1000))
    assert [a.alert_type for a in drop] == [AlertType.VIDEO_DROP]
    # Small movement: neither rule fires.
    calm = engine.on_metric(workspace_id="ws1", account_id="acc1",
                            current=_metric("v-calm", 1100),
                            previous=_metric("v-calm", 1000))
    assert calm == []


def test_alert_one_dead_channel_does_not_block_others():
    class Boom:
        def notify(self, alert):
            raise RuntimeError("webhook down")
    center = NotificationCenter()
    multi = MultiChannelNotifier(channels=[Boom(), center])
    engine = AlertRuleEngine(multi)
    alert = engine.on_proxy_dead(workspace_id="ws1", proxy_id="px9")
    assert alert is not None
    assert len(center.alerts) == 1             # healthy channel still got it
    assert len(multi.errors) == 1              # failure recorded, not raised


# ------------------------------------------------------- collect_sample_data
_SECRET_KEYS = {"access_token", "refresh_token", "api_key", "client_secret",
                "password", "secret"}


def _walk_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k).lower()
            yield from _walk_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _walk_keys(item)


def test_collect_sample_data_golden_cases_cover_all_platforms():
    mod = _load_collect()
    cases = mod.build_golden_cases()
    assert {c["platform"] for c in cases} == {"facebook", "tiktok", "youtube"}
    expected_views = {"facebook": 12000, "tiktok": 50000, "youtube": 98000}
    for case in cases:
        fixture = REPO_ROOT / case["raw_fixture_path"]
        assert fixture.exists(), f"missing golden fixture: {fixture}"
        on_disk = json.loads(fixture.read_text(encoding="utf-8"))
        # The golden contract: the fixture ON DISK normalizes exactly to
        # the case's expected metric (not just the in-memory builder).
        assert mod.normalize(case["platform"], on_disk) == \
            case["expected_first_metric"]
        assert case["expected_first_metric"]["views"] == \
            expected_views[case["platform"]]
    # Benchmark is part of the generator contract and must be clean.
    rows = mod.benchmark(iterations=5)
    assert len(rows) == 3 and all(r["success_rate"] == 1.0 for r in rows)


def test_collect_sample_data_stores_no_real_credentials():
    mod = _load_collect()
    blob = {"fixtures": {p: b() for p, b in mod.RAW_BUILDERS.items()},
            "cases": mod.build_golden_cases()}
    keys = set(_walk_keys(blob))
    assert not (keys & _SECRET_KEYS), f"secret-like keys in fixtures: {keys}"
    text = json.dumps(blob)
    assert "example.invalid" in text           # synthetic hosts only
    assert "Bearer " not in text and "sk-" not in text


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    for name, fn in fns:
        fn()
        print(f"PASS {name}")
    print(f"ALL TESTS PASSED ({len(fns)} tests)")
