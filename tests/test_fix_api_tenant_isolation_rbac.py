"""Tests for fix_api_tenant_isolation_rbac (backend-sec).

Tenant isolation from JWT claims, real /auth/login (password + TOTP),
RBAC + audit, WebSocket handshake auth, narrow CORS, fail-fast secret,
no fake jobs without a scheduler.
Run from repo root: python -m pytest tests/test_fix_api_tenant_isolation_rbac.py -q
"""
import base64
import hashlib
import hmac
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from fastapi.testclient import TestClient

from src.api.main import InMemoryStore, create_app
from src.contracts import Platform, PlatformAccount, ProxyInfo, Video
from src.security.rbac_audit import generate_totp_secret, hash_password, verify_totp

SECRET = "test-secret-do-not-use-in-prod"


def _jwt(payload, secret=SECRET):
    def b64(b):
        return base64.urlsafe_b64encode(b).decode().rstrip("=")
    header = b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = b64(json.dumps({**payload, "exp": int(time.time()) + 3600}).encode())
    sig = b64(hmac.new(secret.encode(), f"{header}.{body}".encode(),
                       hashlib.sha256).digest())
    return f"{header}.{body}.{sig}"


def _headers(ws="ws-a", role="admin", sub="u1"):
    return {"Authorization": "Bearer " + _jwt(
        {"sub": sub, "workspace_id": ws, "role": role})}


def _store_with_accounts():
    store = InMemoryStore()
    store.accounts["a1"] = PlatformAccount(
        id="a1", workspace_id="ws-a", platform=Platform.YOUTUBE,
        platform_user_id="chan-a", display_name="A")
    store.accounts["b1"] = PlatformAccount(
        id="b1", workspace_id="ws-b", platform=Platform.TIKTOK,
        platform_user_id="user-b", display_name="B")
    store.videos["v1"] = Video(id="v1", account_id="b1", platform=Platform.TIKTOK,
                                platform_video_id="pv-b", title="B video")
    return store


def _client(store=None, **kw):
    return TestClient(create_app(store=store or InMemoryStore(),
                                 jwt_secret=SECRET, **kw))


# ---------------------------------------------------------------- fail-fast
def test_create_app_without_secret_fails_fast():
    with pytest.raises(RuntimeError):
        create_app()


def test_missing_and_bad_token_401():
    c = _client()
    assert c.get("/accounts").status_code == 401
    bad = {"Authorization": "Bearer not-a-token"}
    assert c.get("/accounts", headers=bad).status_code == 401


# ---------------------------------------------------------------- tenant isolation
def test_list_accounts_uses_claims_workspace_only():
    c = _client(_store_with_accounts())
    r = c.get("/accounts", headers=_headers("ws-a"))
    assert r.status_code == 200
    ids = [i["id"] for i in r.json()["items"]]
    assert ids == ["a1"]  # never tenant B's account


def test_list_accounts_mismatched_workspace_param_403():
    c = _client(_store_with_accounts())
    r = c.get("/accounts?workspace_id=ws-b", headers=_headers("ws-a"))
    assert r.status_code == 403
    r2 = c.get("/accounts?workspace_id=ws-a", headers=_headers("ws-a"))
    assert r2.status_code == 200


def test_get_and_delete_other_tenant_403():
    store = _store_with_accounts()
    c = _client(store)
    assert c.get("/accounts/b1", headers=_headers("ws-a")).status_code == 403
    assert c.delete("/accounts/b1", headers=_headers("ws-a")).status_code == 403
    assert "b1" in store.accounts  # not deleted


def test_create_account_workspace_comes_from_claims():
    store = InMemoryStore()
    c = _client(store)
    r = c.post("/accounts", headers=_headers("ws-a"), json={
        "platform": "youtube", "platform_user_id": "x1",
        "credential_ref": "vault://x"})
    assert r.status_code == 201
    assert r.json()["workspace_id"] == "ws-a"
    # Body trying to plant the account in another workspace -> 403
    r2 = c.post("/accounts", headers=_headers("ws-a"), json={
        "workspace_id": "ws-b", "platform": "youtube",
        "platform_user_id": "x2", "credential_ref": "vault://y"})
    assert r2.status_code == 403
    assert all(a.workspace_id == "ws-a" for a in store.accounts.values())


def test_videos_and_metrics_filtered_by_tenant():
    c = _client(_store_with_accounts())
    r = c.get("/videos", headers=_headers("ws-a"))
    assert r.json()["items"] == []  # v1 belongs to tenant B
    assert c.get("/videos/v1/metrics", headers=_headers("ws-a")).status_code == 403
    assert c.get("/videos", headers=_headers("ws-b")).json()["items"][0]["id"] == "v1"


def test_sync_other_tenant_403_and_no_scheduler_503_no_fake_job():
    store = _store_with_accounts()
    c = _client(store)  # no scheduler injected
    assert c.post("/accounts/b1/sync-videos",
                  headers=_headers("ws-a")).status_code == 403
    r = c.post("/accounts/a1/sync-videos", headers=_headers("ws-a"))
    assert r.status_code == 503
    assert store.jobs == {}  # no PENDING fake job was created


# ---------------------------------------------------------------- RBAC + audit
def test_viewer_cannot_create_delete_or_sync():
    store = _store_with_accounts()
    c = _client(store)
    hv = _headers("ws-a", role="viewer")
    assert c.get("/accounts", headers=hv).status_code == 200
    assert c.post("/accounts", headers=hv, json={
        "platform": "youtube", "platform_user_id": "z",
        "credential_ref": "vault://z"}).status_code == 403
    assert c.delete("/accounts/a1", headers=hv).status_code == 403
    assert c.post("/accounts/a1/sync-videos", headers=hv).status_code == 403


def test_sensitive_actions_are_audited():
    store = InMemoryStore()
    app = create_app(store=store, jwt_secret=SECRET)
    c = TestClient(app)
    r = c.post("/accounts", headers=_headers("ws-a"), json={
        "platform": "youtube", "platform_user_id": "x1",
        "credential_ref": "vault://x"})
    assert r.status_code == 201
    acc_id = r.json()["id"]
    assert c.delete(f"/accounts/{acc_id}", headers=_headers("ws-a")).status_code == 204
    actions = [e.action for e in app.state.audit.entries(workspace_id="ws-a")]
    assert "account.create" in actions and "account.delete" in actions
    assert app.state.audit.verify_chain()


def test_assign_proxy_tenant_and_permission_and_audit():
    store = _store_with_accounts()
    store.proxies["p1"] = ProxyInfo(id="p1", host="1.2.3.4", port=8080)
    app = create_app(store=store, jwt_secret=SECRET)
    c = TestClient(app)
    # other tenant's account
    assert c.post("/accounts/b1/assign-proxy", headers=_headers("ws-a"),
                  json={"proxy_id": "p1"}).status_code == 403
    # operator lacks PROXY_ASSIGN
    assert c.post("/accounts/a1/assign-proxy", headers=_headers("ws-a", role="operator"),
                  json={"proxy_id": "p1"}).status_code == 403
    # manager may assign
    r = c.post("/accounts/a1/assign-proxy", headers=_headers("ws-a", role="manager"),
               json={"proxy_id": "p1"})
    assert r.status_code == 200
    assert any(e.action == "proxy.assign"
               for e in app.state.audit.entries(workspace_id="ws-a"))


# ---------------------------------------------------------------- login thật
def _users():
    totp = generate_totp_secret()
    return {
        "alice": {"id": "u-alice", "workspace_id": "ws-a", "role": "manager",
                  "password_hash": hash_password("correct horse")},
        "root": {"id": "u-root", "workspace_id": "ws-a", "role": "admin",
                 "password_hash": hash_password("admin pass"),
                 "totp_secret": totp},
    }, totp


def test_login_real_password_and_jwt_works():
    users, _ = _users()
    c = _client(users=users)
    r = c.post("/auth/login", json={"username": "alice", "password": "wrong"})
    assert r.status_code == 401
    r = c.post("/auth/login", json={"username": "alice", "password": "correct horse"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    assert r.json()["workspace_id"] == "ws-a"
    h = {"Authorization": f"Bearer {token}"}
    assert c.get("/accounts", headers=h).status_code == 200


def test_login_admin_requires_valid_totp():
    users, totp = _users()
    c = _client(users=users)
    r = c.post("/auth/login", json={"username": "root", "password": "admin pass"})
    assert r.status_code == 401  # no TOTP code
    # find a currently valid code via rbac helper (window +/-1)
    now = time.time()
    code = None
    for cand in range(1_000_000):
        s = f"{cand:06d}"
        if verify_totp(totp, s, at=now):
            code = s
            break
    assert code
    r = c.post("/auth/login", json={"username": "root", "password": "admin pass",
                                    "totp_code": code})
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_login_unknown_user_generic_401():
    users, _ = _users()
    c = _client(users=users)
    r = c.post("/auth/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401
    assert r.json()["detail"] == "invalid credentials"


# ---------------------------------------------------------------- CORS + WebSocket
def test_cors_is_not_wildcard():
    c = _client()
    r = c.get("/health", headers={"Origin": "https://evil.example"})
    assert r.headers.get("access-control-allow-origin") != "*"


def test_websocket_requires_auth_and_accepts_valid_token():
    c = _client(_store_with_accounts())
    with pytest.raises(Exception):
        with c.websocket_connect("/ws/events"):
            pass
    token = _jwt({"sub": "u1", "workspace_id": "ws-a", "role": "admin"})
    with c.websocket_connect(f"/ws/events?token={token}") as ws:
        ws.send_text("ping")  # connection stays open = handshake accepted
