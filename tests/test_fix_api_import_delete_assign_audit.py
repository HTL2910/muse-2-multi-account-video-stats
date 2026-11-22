"""Tests for fix_api_import_delete_assign_audit (backend-sec).

Import upsert by natural key, soft-delete with credential revocation +
append-only audit, assign-proxy workspace check + ProxyBinding sticky 1-1.
Run from repo root: python -m pytest tests/test_fix_api_import_delete_assign_audit.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from src.api.main import InMemoryStore, create_app, verify_audit_chain
from src.contracts import AccountStatus, Platform, PlatformAccount, ProxyInfo


def _checker(token: str):
    # Token format in tests: "ws-a" / "ws-b" -> claims for that workspace
    ws = token.replace("Bearer ", "").strip() or "ws-a"
    return {"sub": "tester", "workspace_id": ws}


def _h(ws="ws-a"):
    return {"Authorization": f"Bearer {ws}"}


def _client(store=None):
    store = store or InMemoryStore()
    return TestClient(create_app(store=store, auth_checker=_checker)), store


def _payload(ws="ws-a", uid="chan-1", name="Channel One"):
    return {"workspace_id": ws, "platform": "youtube", "platform_user_id": uid,
            "display_name": name, "credential_ref": "vault://cred-1"}


# ---------------------------------------------------------------- import dedup
def test_import_twice_creates_only_one_account():
    c, store = _client()
    r1 = c.post("/accounts/import", headers=_h(), json=[_payload()])
    assert r1.status_code == 201
    assert r1.json()["count"] == 1 and len(r1.json()["created"]) == 1
    r2 = c.post("/accounts/import", headers=_h(), json=[_payload()])
    assert r2.status_code == 201
    assert r2.json()["created"] == [] and r2.json()["count"] == 0
    assert r2.json()["updated"] == r1.json()["created"]
    assert len(store.accounts) == 1


def test_import_dedup_within_payload_and_updates_fields():
    c, store = _client()
    r = c.post("/accounts/import", headers=_h(),
               json=[_payload(), _payload(name="Renamed")])
    assert len(store.accounts) == 1
    acc = next(iter(store.accounts.values()))
    assert acc.display_name == "Renamed"  # second occurrence upserted
    assert r.json()["total"] == 2


def test_import_same_user_other_workspace_is_separate():
    c, store = _client()
    c.post("/accounts/import", headers=_h("ws-a"), json=[_payload("ws-a")])
    c.post("/accounts/import", headers=_h("ws-b"), json=[_payload("ws-b")])
    assert len(store.accounts) == 2


def test_import_after_soft_delete_does_not_duplicate_or_revive():
    c, store = _client()
    c.post("/accounts/import", headers=_h(), json=[_payload()])
    acc_id = next(iter(store.accounts))
    assert c.delete(f"/accounts/{acc_id}", headers=_h()).status_code == 204
    c.post("/accounts/import", headers=_h(), json=[_payload()])
    assert len(store.accounts) == 1
    assert store.accounts[acc_id].status == AccountStatus.DISABLED


# ---------------------------------------------------------------- delete cascade + audit
def test_delete_is_soft_revokes_credential_and_audits():
    c, store = _client()
    c.post("/accounts/import", headers=_h(), json=[_payload()])
    acc_id = next(iter(store.accounts))
    r = c.delete(f"/accounts/{acc_id}", headers=_h())
    assert r.status_code == 204
    acc = store.accounts[acc_id]  # still present: NOT hard-deleted
    assert acc.status == AccountStatus.DISABLED
    assert acc.credential_ref == ""  # credential revoked
    entries = [e for e in store.audit_log if e.action == "account.delete"]
    assert len(entries) == 1
    assert entries[0].target_id == acc_id
    assert entries[0].detail["credential_revoked"] is True
    assert "vault://cred-1" not in str(entries[0].detail)  # ref itself not leaked
    assert verify_audit_chain(store)
    # idempotent re-delete: still 204, no duplicate audit entry
    assert c.delete(f"/accounts/{acc_id}", headers=_h()).status_code == 204
    assert len([e for e in store.audit_log if e.action == "account.delete"]) == 1


def test_delete_closes_proxy_binding():
    c, store = _client()
    c.post("/accounts/import", headers=_h(), json=[_payload()])
    acc_id = next(iter(store.accounts))
    store.proxies["p1"] = ProxyInfo(id="p1", host="1.2.3.4", port=8080)
    r = c.post(f"/accounts/{acc_id}/assign-proxy", headers=_h(), json={"proxy_id": "p1"})
    binding_id = r.json()["binding_id"]
    assert c.delete(f"/accounts/{acc_id}", headers=_h()).status_code == 204
    binding = store.proxy_bindings[binding_id]
    assert binding.unbound_at is not None and binding.reason == "account_deleted"
    assert store.accounts[acc_id].proxy_binding_id is None


def test_delete_missing_account_404():
    c, _ = _client()
    assert c.delete("/accounts/nope", headers=_h()).status_code == 404


# ---------------------------------------------------------------- assign-proxy
def _setup_account(store, ws="ws-a"):
    store.accounts["a1"] = PlatformAccount(
        id="a1", workspace_id=ws, platform=Platform.YOUTUBE,
        platform_user_id="chan-1", credential_ref="vault://c")


def test_assign_proxy_creates_binding_and_points_account_at_it():
    c, store = _client()
    _setup_account(store)
    store.proxies["p1"] = ProxyInfo(id="p1", host="1.2.3.4", port=8080)
    r = c.post("/accounts/a1/assign-proxy", headers=_h(), json={"proxy_id": "p1"})
    assert r.status_code == 200
    binding_id = r.json()["binding_id"]
    assert store.accounts["a1"].proxy_binding_id == binding_id
    b = store.proxy_bindings[binding_id]
    assert b.account_id == "a1" and b.proxy_id == "p1" and b.unbound_at is None
    assert any(e.action == "proxy.assign" for e in store.audit_log)


def test_assign_proxy_other_workspace_proxy_403():
    c, store = _client()
    _setup_account(store, ws="ws-a")
    # proxy imported while authenticated as ws-b -> owned by ws-b
    r = c.post("/proxies/import", headers=_h("ws-b"),
               json=[{"host": "5.6.7.8", "port": 3128}])
    proxy_id = r.json()["created"][0]
    r2 = c.post("/accounts/a1/assign-proxy", headers=_h("ws-a"),
                json={"proxy_id": proxy_id})
    assert r2.status_code == 403
    assert store.proxy_bindings == {}
    assert store.accounts["a1"].proxy_binding_id is None


def test_assign_proxy_account_of_other_workspace_403():
    c, store = _client()
    _setup_account(store, ws="ws-b")
    store.proxies["p1"] = ProxyInfo(id="p1", host="1.2.3.4", port=8080)
    r = c.post("/accounts/a1/assign-proxy", headers=_h("ws-a"),
               json={"proxy_id": "p1"})
    assert r.status_code == 403


def test_assign_proxy_reassign_closes_old_binding_sticky_1_1():
    c, store = _client()
    _setup_account(store)
    store.accounts["a2"] = PlatformAccount(
        id="a2", workspace_id="ws-a", platform=Platform.TIKTOK,
        platform_user_id="u2", credential_ref="vault://c2")
    store.proxies["p1"] = ProxyInfo(id="p1", host="1.2.3.4", port=8080)
    store.proxies["p2"] = ProxyInfo(id="p2", host="9.9.9.9", port=8080)
    b1 = c.post("/accounts/a1/assign-proxy", headers=_h(),
                json={"proxy_id": "p1"}).json()["binding_id"]
    # sticky 1-1: p1 is bound to a1, a2 cannot take it
    r = c.post("/accounts/a2/assign-proxy", headers=_h(), json={"proxy_id": "p1"})
    assert r.status_code == 409
    # reassign a1 -> p2 closes b1 with unbound_at
    b2 = c.post("/accounts/a1/assign-proxy", headers=_h(),
                json={"proxy_id": "p2"}).json()["binding_id"]
    assert b2 != b1
    assert store.proxy_bindings[b1].unbound_at is not None
    assert store.proxy_bindings[b1].reason == "reassigned"
    assert store.proxy_bindings[b2].unbound_at is None
    # idempotent: assigning p2 again returns the same binding
    again = c.post("/accounts/a1/assign-proxy", headers=_h(), json={"proxy_id": "p2"})
    assert again.json()["binding_id"] == b2
    assert len(store.proxy_bindings) == 2
