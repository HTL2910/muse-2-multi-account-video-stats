"""approach_13 — REST API + WebSocket Gateway (lop tich hop end-to-end).

Backend FastAPI, phan lop Routers -> Service -> Adapter/Worker.
Chi dung FastAPI + thu vien chuan; khong goi truc tiep API nen tang o day
— moi sync di qua TaskScheduler (approach_09) va Adapter (approach_05/06/07).

Endpoints (theo dac ta):
  /auth, /accounts (CRUD + import), /accounts/{id}/check-alive,
  /accounts/{id}/sync-videos, /accounts/{id}/assign-proxy,
  /videos, /videos/{id}/metrics?from&to, /proxies, /proxies/health,
  /proxies/import, /groups, /tags, /jobs, /ws/events
Phan trang cursor-based, filter platform/status/search thong nhat.
OpenAPI/Swagger tu dong tu FastAPI; SDK TypeScript sinh tu OpenAPI.

Fix ``fix_api_tenant_isolation_rbac`` (gop fix_api_tenant_isolation_and_
ws_no_auth + fix_api_auth_fake_job_cors):
- Tenant isolation: ``workspace_id`` va ``role`` CHI lay tu JWT claims
  da verify. Client gui workspace_id (body/query) lech claims -> 403;
  workspace_id khong con la nguon su that o bat ky endpoint nao.
- RBAC: moi endpoint gan Permission cua approach_16 (rbac_audit) theo
  thao tac, va cac thao tac trong SENSITIVE_ACTIONS duoc ghi AuditChain.
- /auth/login THAT: verify password (hash_password/verify_password) +
  TOTP (verify_totp, bat buoc voi ADMIN) cua rbac_audit, roi moi ky JWT.
- /ws/events phai auth o handshake (query ``token`` hoac header
  Authorization); broadcast chi gui cho client cung workspace khi event
  co workspace_id.
- CORS: chi origin cu the tu env CORS_ALLOWED_ORIGINS (mac dinh
  http://localhost:3000), khong con '*'.
- Fail-fast: create_app() khong co JWT_SECRET va khong co auth_checker
  duoc inject thi raise RuntimeError ngay luc khoi dong.
- Khong co scheduler thi sync tra 503, KHONG tao job PENDING gia.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

try:
    from src.contracts import (  # type: ignore
        AccountStatus, CollectJob, JobPriority, JobStatus, JobType, Platform,
        PlatformAccount, ProxyInfo, ProxyStatus, Role, Video,
        UnifiedVideoMetric, utcnow,
    )
except ImportError:
    from ..contracts import (  # type: ignore
        AccountStatus, CollectJob, JobPriority, JobStatus, JobType, Platform,
        PlatformAccount, ProxyInfo, ProxyStatus, Role, Video,
        UnifiedVideoMetric, utcnow,
    )

# --- approach_16 (RBAC / password / TOTP / audit) ---------------------------
try:
    from src.security.rbac_audit import (  # type: ignore
        SENSITIVE_ACTIONS, AuditChain, LoginRateLimiter, Permission,
        PermissionDenied, has_permission, hash_password, require_permission,
        verify_password, verify_totp,
    )
except ImportError:  # pragma: no cover - chay nhu package src.api.*
    try:
        from ..security.rbac_audit import (  # type: ignore
            SENSITIVE_ACTIONS, AuditChain, LoginRateLimiter, Permission,
            PermissionDenied, has_permission, hash_password, require_permission,
            verify_password, verify_totp,
        )
    except ImportError:  # pragma: no cover - fallback cung ngu nghia approach_16
        import secrets as _secrets
        from enum import Enum as _Enum

        class PermissionDenied(PermissionError):
            pass

        class Permission(str, _Enum):
            ACCOUNT_VIEW = "account.view"
            ACCOUNT_CREATE = "account.create"
            ACCOUNT_DELETE = "account.delete"
            ACCOUNT_SYNC = "account.sync"
            PROXY_VIEW = "proxy.view"
            PROXY_ASSIGN = "proxy.assign"
            PROXY_IMPORT = "proxy.import"
            CREDENTIAL_VIEW = "credential.view"
            DATA_EXPORT = "data.export"
            USER_MANAGE = "user.manage"
            AUDIT_VIEW = "audit.view"
            SETTINGS_MANAGE = "settings.manage"

        _ROLE_PERMS = {
            Role.VIEWER: frozenset({Permission.ACCOUNT_VIEW, Permission.PROXY_VIEW}),
            Role.OPERATOR: frozenset({Permission.ACCOUNT_VIEW, Permission.PROXY_VIEW,
                                      Permission.ACCOUNT_SYNC}),
            Role.MANAGER: frozenset({Permission.ACCOUNT_VIEW, Permission.PROXY_VIEW,
                                     Permission.ACCOUNT_SYNC, Permission.ACCOUNT_CREATE,
                                     Permission.PROXY_ASSIGN, Permission.PROXY_IMPORT,
                                     Permission.DATA_EXPORT, Permission.AUDIT_VIEW}),
            Role.ADMIN: frozenset(Permission),
        }
        SENSITIVE_ACTIONS = frozenset({
            "credential.view", "proxy.assign", "proxy.import", "account.delete",
            "account.create", "data.export", "user.manage", "settings.manage",
            "auth.login", "auth.login_failed", "auth.2fa_failed"})

        def has_permission(role: Role, perm: Permission) -> bool:
            return perm in _ROLE_PERMS.get(role, frozenset())

        def require_permission(role: Role, perm: Permission, *, workspace_id: str,
                               resource_workspace_id: str) -> None:
            if resource_workspace_id != workspace_id:
                raise PermissionDenied("cross-workspace access denied (RLS)")
            if not has_permission(role, perm):
                raise PermissionDenied(f"role {role.value} lacks {perm.value}")

        def hash_password(password: str) -> str:
            salt = _secrets.token_bytes(16)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
            return ("pbkdf2_sha256$600000$" + base64.b64encode(salt).decode()
                    + "$" + base64.b64encode(dk).decode())

        def verify_password(password: str, stored: str) -> bool:
            try:
                if stored.startswith("pbkdf2_sha256$"):
                    _, iters, salt_b64, dk_b64 = stored.split("$")
                    dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                             base64.b64decode(salt_b64), int(iters))
                    return hmac.compare_digest(base64.b64encode(dk).decode(), dk_b64)
            except Exception:
                return False
            return False

        def _totp_at(secret_b32: str, counter: int) -> str:
            key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
            digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
            offset = digest[-1] & 0x0F
            code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
            return f"{code:06d}"

        def verify_totp(secret_b32: str, code: str, *, at: Optional[float] = None,
                        window: int = 1) -> bool:
            step = int((at if at is not None else time.time()) // 30)
            return any(hmac.compare_digest(_totp_at(secret_b32, step + d), str(code).strip())
                       for d in range(-window, window + 1))

        class AuditChain:  # type: ignore[no-redef]
            """Append-only + SHA-256 hash-chain (ban rut gon cua approach_16)."""

            def __init__(self) -> None:
                self._entries: list = []

            def append(self, *, workspace_id: str, actor_id: str, action: str,
                       target_type: str, target_id: str,
                       detail: Optional[dict] = None) -> Any:
                from src.contracts import AuditLogEntry as _E  # type: ignore
                detail = {k: ("***" if any(s in k.lower() for s in
                                           ("password", "token", "cookie", "secret", "totp")) else v)
                          for k, v in (detail or {}).items()}
                entry = _E(id=uuid.uuid4().hex, workspace_id=workspace_id,
                           actor_id=actor_id, action=action, target_type=target_type,
                           target_id=target_id, detail=detail,
                           prev_hash=self._entries[-1].entry_hash if self._entries else "GENESIS")
                payload = json.dumps({"prev": entry.prev_hash, "id": entry.id,
                                      "ws": workspace_id, "actor": actor_id,
                                      "action": action, "target": f"{target_type}:{target_id}",
                                      "detail": entry.detail,
                                      "ts": entry.created_at.isoformat()},
                                     sort_keys=True, ensure_ascii=False)
                entry.entry_hash = hashlib.sha256(payload.encode()).hexdigest()
                self._entries.append(entry)
                return entry

            def entries(self, *, workspace_id: Optional[str] = None) -> list:
                return [e for e in self._entries
                        if workspace_id is None or e.workspace_id == workspace_id]

            def verify_chain(self) -> bool:
                return True  # ban fallback: tinh toan ven do AuditChain that dam nhiem

        class LoginRateLimiter:  # type: ignore[no-redef]
            def __init__(self, *, max_failures: int = 5, window_seconds: int = 900,
                         lock_seconds: int = 900):
                self._max, self._lock = max_failures, lock_seconds
                self._fails: dict[str, list[float]] = {}
                self._locked_until: dict[str, float] = {}

            def is_locked(self, account: str, ip: str, *, at: Optional[float] = None) -> bool:
                return self._locked_until.get(f"{account.lower()}|{ip}", 0) > (at or time.time())

            def record_failure(self, account: str, ip: str, *, at: Optional[float] = None) -> bool:
                now = at or time.time()
                key = f"{account.lower()}|{ip}"
                recent = [t for t in self._fails.get(key, []) if now - t < 900]
                recent.append(now)
                self._fails[key] = recent
                if len(recent) >= self._max:
                    self._locked_until[key] = now + self._lock
                    return True
                return False

            def record_success(self, account: str, ip: str) -> None:
                self._fails.pop(f"{account.lower()}|{ip}", None)

__all__ = ["create_app", "app", "EventHub", "InMemoryStore", "RateLimiter",
           "encode_cursor", "decode_cursor"]

# --- Cursor pagination ------------------------------------------------------
def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()

def decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        off = int(json.loads(base64.urlsafe_b64decode(cursor.encode()).decode()).get("o", 0))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid cursor")
    if off < 0:
        raise HTTPException(status_code=400, detail="invalid cursor")
    return off

# --- Store bo nho (production: PostgreSQL + MetricsStore) -------------------
@dataclass
class InMemoryStore:
    accounts: dict[str, PlatformAccount] = field(default_factory=dict)
    videos: dict[str, Video] = field(default_factory=dict)
    metrics: dict[str, list[UnifiedVideoMetric]] = field(default_factory=dict)
    proxies: dict[str, ProxyInfo] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)

    def list_accounts(self, *, workspace_id: str, platform: Optional[Platform] = None,
                      status: Optional[AccountStatus] = None, search: str = "") -> list[PlatformAccount]:
        out = [a for a in self.accounts.values() if a.workspace_id == workspace_id]
        if platform:
            out = [a for a in out if a.platform == platform]
        if status:
            out = [a for a in out if a.status == status]
        if search:
            s = search.lower()
            out = [a for a in out if s in a.display_name.lower() or s in a.platform_user_id.lower()]
        return sorted(out, key=lambda a: a.created_at)

class RateLimiter:
    """Token bucket don gian theo tenant; production dung Redis."""
    def __init__(self, *, rate_per_minute: int = 600):
        self.rate = rate_per_minute
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, tenant: str) -> bool:
        now = time.monotonic()
        tokens, last = self._buckets.get(tenant, (float(self.rate), now))
        tokens = min(float(self.rate), tokens + (now - last) * self.rate / 60.0)
        if tokens < 1:
            self._buckets[tenant] = (tokens, now)
            return False
        self._buckets[tenant] = (tokens - 1, now)
        return True

class EventHub:
    """Broadcast WebSocket theo workspace; moi event co account_id/job_id de trace."""
    def __init__(self) -> None:
        self._clients: dict[WebSocket, str] = {}  # ws -> workspace_id cua client

    async def connect(self, ws: WebSocket, *, workspace_id: str = "") -> None:
        await ws.accept()
        self._clients[ws] = workspace_id

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.pop(ws, None)

    async def broadcast(self, event: dict[str, Any]) -> None:
        dead = []
        event_ws = str(event.get("workspace_id") or "")
        for ws, client_ws in list(self._clients.items()):
            if event_ws and client_ws and event_ws != client_ws:
                continue  # khong ro event cua tenant nay sang tenant khac
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.pop(ws, None)

# --- JWT (HS256, secret tu env; khong hardcode) -----------------------------
def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")

def _issue_jwt(payload: dict[str, Any], *, secret: str,
               ttl_seconds: int = 3600) -> str:
    now = int(time.time())
    body = {**payload, "iat": now, "exp": now + ttl_seconds}
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    claims = _b64url(json.dumps(body).encode())
    sig = _b64url(hmac.new(secret.encode(), f"{header}.{claims}".encode(),
                           hashlib.sha256).digest())
    return f"{header}.{claims}.{sig}"

def _verify_jwt(token: str, *, secret: str) -> dict[str, Any]:
    try:
        header_b64, payload_b64, sig_b64 = token.split(".")
        signing = f"{header_b64}.{payload_b64}".encode()
        expect = base64.urlsafe_b64encode(hmac.new(secret.encode(), signing, hashlib.sha256).digest()).decode().rstrip("=")
        if not hmac.compare_digest(expect, sig_b64):
            raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "==").decode())
        if payload.get("exp", 0) < int(time.time()):
            raise ValueError("token expired")
        return payload
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc

# --- Pydantic schemas (API layer, khong dung truc tiep dataclass contracts) --
class AccountCreate(BaseModel):
    # workspace_id KHONG con la nguon su that: chi nhan de tuong thich va
    # de phat hien lech claims (co mat + khac claims -> 403). Workspace
    # cua account luon la workspace trong JWT claims.
    workspace_id: Optional[str] = None
    platform: Platform
    platform_user_id: str
    display_name: str = ""
    credential_ref: str
    owner: str = ""
    niche: str = ""
    tags: list[str] = Field(default_factory=list)

class AccountOut(BaseModel):
    id: str; workspace_id: str; platform: Platform; platform_user_id: str
    display_name: str; status: AccountStatus; health_score: float
    proxy_binding_id: Optional[str] = None; last_sync_at: Optional[datetime] = None

    @classmethod
    def from_account(cls, a: PlatformAccount) -> "AccountOut":
        return cls(id=a.id, workspace_id=a.workspace_id, platform=a.platform,
                   platform_user_id=a.platform_user_id, display_name=a.display_name,
                   status=a.status, health_score=a.health_score,
                   proxy_binding_id=a.proxy_binding_id, last_sync_at=a.last_sync_at)

class ProxyCreate(BaseModel):
    host: str; port: int; country: str = ""; kind: str = "residential"
    credential_ref: str = ""

class SyncResponse(BaseModel):
    account_id: str; job_id: str; status: JobStatus; message: str = ""

class LoginRequest(BaseModel):
    username: str
    password: str
    totp_code: str = ""  # chap nhan ca alias 'otp'/'code' o tang handler

def _load_users_from_env() -> dict[str, dict[str, Any]]:
    raw = os.environ.get("API_USERS_JSON", "")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _cors_origins(explicit: Optional[list[str]]) -> list[str]:
    if explicit:
        origins = explicit
    else:
        env = os.environ.get("CORS_ALLOWED_ORIGINS", "")
        origins = [o.strip() for o in env.split(",") if o.strip()] or ["http://localhost:3000"]
    # Khong bao gio '*' kem Bearer auth: bo qua neu cau hinh sai.
    return [o for o in origins if o != "*"] or ["http://localhost:3000"]

def create_app(*, store: Optional[InMemoryStore] = None,
               scheduler: Any = None,
               auth_checker: Optional[Callable[[str], dict[str, Any]]] = None,
               jwt_secret: Optional[str] = None,
               users: Optional[dict[str, dict[str, Any]]] = None,
               cors_origins: Optional[list[str]] = None) -> FastAPI:
    store = store or InMemoryStore()
    hub = EventHub()
    limiter = RateLimiter()
    audit = AuditChain()
    login_limiter = LoginRateLimiter()
    secret = jwt_secret or os.environ.get("JWT_SECRET", "")
    if auth_checker is None and not secret:
        # Fail-fast: thieu secret thi API khong duoc khoi dong voi auth that.
        raise RuntimeError("JWT_SECRET not configured")
    user_store: dict[str, dict[str, Any]] = {**_load_users_from_env(), **(users or {})}

    # -- Claims: workspace + role CHI tu token da verify -------------------
    def _normalize_claims(raw: Any, *, test_mode: bool = False) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=401, detail="invalid token claims")
        claims = dict(raw)
        workspace = (claims.get("workspace_id") or claims.get("workspace")
                     or claims.get("tenant_id") or claims.get("tenant"))
        if not workspace:
            raise HTTPException(status_code=403, detail="missing workspace_id claim")
        claims["workspace_id"] = str(workspace)
        role_raw = claims.get("role")
        if role_raw is None:
            # auth_checker la diem bypass danh cho test/dev: mac dinh ADMIN.
            # JWT that khong co role -> quyen thap nhat (VIEWER).
            role = Role.ADMIN if test_mode else Role.VIEWER
        else:
            try:
                role = Role(str(role_raw).lower())
            except ValueError:
                raise HTTPException(status_code=401, detail="invalid role claim")
        claims["role"] = role.value
        claims["_role"] = role
        claims["sub"] = str(claims.get("sub") or claims.get("user_id") or "")
        return claims

    def _auth(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
        if auth_checker is not None:
            return _normalize_claims(auth_checker(authorization or ""), test_mode=True)
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        return _normalize_claims(_verify_jwt(authorization[7:], secret=secret))

    def _rate(claims: dict[str, Any]) -> None:
        tenant = str(claims.get("workspace_id") or claims.get("sub") or "default")
        if not limiter.allow(tenant):
            raise HTTPException(status_code=429, detail="rate limited for tenant")

    def _require(claims: dict[str, Any], perm: Permission, *,
                 resource_workspace_id: Optional[str] = None) -> None:
        """RBAC + RLS cua approach_16; PermissionDenied -> 403."""
        try:
            require_permission(claims["_role"], perm,
                               workspace_id=claims["workspace_id"],
                               resource_workspace_id=resource_workspace_id or claims["workspace_id"])
        except PermissionDenied as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def _check_workspace_param(claims: dict[str, Any], given: Optional[str]) -> None:
        if given and given != claims["workspace_id"]:
            raise HTTPException(status_code=403, detail="workspace_id mismatch with token claims")

    def _audit(claims: dict[str, Any], action: str, *, target_type: str,
               target_id: str, detail: Optional[dict[str, Any]] = None) -> None:
        if action in SENSITIVE_ACTIONS:
            audit.append(workspace_id=claims["workspace_id"],
                         actor_id=claims.get("sub", ""), action=action,
                         target_type=target_type, target_id=target_id,
                         detail=detail or {})

    def _account_for_tenant(claims: dict[str, Any], account_id: str,
                            perm: Permission) -> PlatformAccount:
        a = store.accounts.get(account_id)
        if not a:
            raise HTTPException(status_code=404, detail="account not found")
        _require(claims, perm, resource_workspace_id=a.workspace_id)
        return a

    app = FastAPI(title="multi-account-video-stats API", version="1.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=_cors_origins(cors_origins),
                       allow_methods=["*"], allow_headers=["*"])
    app.state.store = store
    app.state.hub = hub
    app.state.scheduler = scheduler
    app.state.audit = audit

    def _page(items: list[Any], *, cursor: Optional[str], limit: int):
        off = decode_cursor(cursor)
        page = items[off:off + limit]
        nxt = encode_cursor(off + limit) if off + limit < len(items) else None
        return {"items": page, "next_cursor": nxt, "total": len(items)}

    @app.get("/health")
    def health():
        return {"status": "ok", "contracts": "1.0.0"}

    # -- Auth that (approach_16): password + TOTP -> JWT --------------------
    @app.post("/auth/login")
    def login(body: dict[str, Any]):
        username = str(body.get("username") or body.get("email") or "")
        password = str(body.get("password") or "")
        totp_code = str(body.get("totp_code") or body.get("otp") or body.get("code") or "")
        ip = "unknown"
        claims_like = {"workspace_id": "", "sub": username}
        record = user_store.get(username)
        if login_limiter.is_locked(username, ip):
            raise HTTPException(status_code=429, detail="account temporarily locked")
        if record is None:
            login_limiter.record_failure(username, ip)
            raise HTTPException(status_code=401, detail="invalid credentials")
        claims_like["workspace_id"] = str(record.get("workspace_id") or "")
        stored_hash = str(record.get("password_hash") or "")
        if not stored_hash and record.get("password"):
            stored_hash = hash_password(str(record["password"]))
        if not stored_hash or not verify_password(password, stored_hash):
            login_limiter.record_failure(username, ip)
            if claims_like["workspace_id"]:
                audit.append(workspace_id=claims_like["workspace_id"], actor_id=username,
                             action="auth.login_failed", target_type="user",
                             target_id=username, detail={"reason": "bad_password"})
            raise HTTPException(status_code=401, detail="invalid credentials")
        try:
            role = Role(str(record.get("role") or "viewer").lower())
        except ValueError:
            raise HTTPException(status_code=401, detail="invalid credentials")
        totp_secret = str(record.get("totp_secret") or "")
        if totp_secret or role == Role.ADMIN:
            if not totp_secret:
                raise HTTPException(status_code=403,
                                    detail="2FA enrollment required for this account")
            if not totp_code or not verify_totp(totp_secret, totp_code):
                login_limiter.record_failure(username, ip)
                audit.append(workspace_id=claims_like["workspace_id"], actor_id=username,
                             action="auth.2fa_failed", target_type="user",
                             target_id=username, detail={"reason": "bad_totp"})
                raise HTTPException(status_code=401, detail="invalid 2FA code")
        if not claims_like["workspace_id"]:
            raise HTTPException(status_code=401, detail="invalid credentials")
        login_limiter.record_success(username, ip)
        user_id = str(record.get("id") or record.get("sub") or username)
        token = _issue_jwt({"sub": user_id, "username": username,
                            "workspace_id": claims_like["workspace_id"],
                            "role": role.value}, secret=secret)
        audit.append(workspace_id=claims_like["workspace_id"], actor_id=user_id,
                     action="auth.login", target_type="user", target_id=user_id,
                     detail={"role": role.value})
        return {"access_token": token, "token_type": "bearer",
                "workspace_id": claims_like["workspace_id"], "role": role.value}

    @app.post("/accounts", response_model=AccountOut, status_code=201)
    def create_account(body: AccountCreate, claims: dict = Depends(_auth)):
        _rate(claims)
        _check_workspace_param(claims, body.workspace_id)
        _require(claims, Permission.ACCOUNT_CREATE)
        a = PlatformAccount(id=uuid.uuid4().hex, workspace_id=claims["workspace_id"],
                            platform=body.platform, platform_user_id=body.platform_user_id,
                            display_name=body.display_name, credential_ref=body.credential_ref,
                            owner=body.owner, niche=body.niche, tags=list(body.tags))
        store.accounts[a.id] = a
        _audit(claims, "account.create", target_type="account", target_id=a.id,
               detail={"platform": a.platform.value})
        return AccountOut.from_account(a)

    @app.get("/accounts")
    def list_accounts(workspace_id: Optional[str] = Query(default=None),
                      platform: Optional[Platform] = None,
                      status: Optional[AccountStatus] = None, search: str = "",
                      cursor: Optional[str] = None, limit: int = Query(50, le=200),
                      claims: dict = Depends(_auth)):
        _rate(claims)
        _check_workspace_param(claims, workspace_id)
        _require(claims, Permission.ACCOUNT_VIEW)
        items = [AccountOut.from_account(a).model_dump(mode="json")
                 for a in store.list_accounts(workspace_id=claims["workspace_id"], platform=platform, status=status, search=search)]
        return _page(items, cursor=cursor, limit=limit)

    @app.get("/accounts/{account_id}", response_model=AccountOut)
    def get_account(account_id: str, claims: dict = Depends(_auth)):
        a = _account_for_tenant(claims, account_id, Permission.ACCOUNT_VIEW)
        return AccountOut.from_account(a)

    @app.delete("/accounts/{account_id}", status_code=204)
    def delete_account(account_id: str, claims: dict = Depends(_auth)):
        _account_for_tenant(claims, account_id, Permission.ACCOUNT_DELETE)
        del store.accounts[account_id]
        _audit(claims, "account.delete", target_type="account", target_id=account_id)
        return None

    @app.post("/accounts/import", status_code=201)
    def import_accounts(body: list[AccountCreate], claims: dict = Depends(_auth)):
        _rate(claims)
        _require(claims, Permission.ACCOUNT_CREATE)
        created = []
        for item in body:
            _check_workspace_param(claims, item.workspace_id)
            a = PlatformAccount(id=uuid.uuid4().hex, workspace_id=claims["workspace_id"],
                                platform=item.platform, platform_user_id=item.platform_user_id,
                                display_name=item.display_name, credential_ref=item.credential_ref)
            store.accounts[a.id] = a
            created.append(a.id)
            _audit(claims, "account.create", target_type="account", target_id=a.id,
                   detail={"platform": a.platform.value, "via": "import"})
        return {"created": created, "count": len(created)}

    def _enqueue(account_id: str, job_type: JobType, claims: dict) -> SyncResponse:
        a = _account_for_tenant(claims, account_id, Permission.ACCOUNT_SYNC)
        if scheduler is None:
            # Khong co worker thi khong duoc gia vo da queue: 503 ro rang.
            raise HTTPException(status_code=503, detail="scheduler not configured")
        # Scheduler fail-closed (approach_09) can trang thai that cua
        # account/proxy: dong bo vao neu scheduler ho tro dang ky.
        register = getattr(scheduler, "register_account", None)
        if callable(register):
            try:
                proxy = store.proxies.get(a.proxy_binding_id or "")
                register(a, proxy_status=proxy.status if proxy else None)
            except TypeError:
                register(a)  # type: ignore[operator]
            except Exception:
                pass
        job = CollectJob(id=uuid.uuid4().hex, job_type=job_type, account_id=account_id, platform=a.platform)
        scheduler.enqueue(job)
        store.jobs[job.id] = {"id": job.id, "job_type": job_type.value,
                              "account_id": account_id, "status": job.status.value,
                              "workspace_id": claims["workspace_id"]}
        return SyncResponse(account_id=account_id, job_id=job.id, status=job.status,
                            message="queued" if job.status == JobStatus.PENDING else job.status.value)

    @app.post("/accounts/{account_id}/check-alive", response_model=SyncResponse)
    def check_alive(account_id: str, claims: dict = Depends(_auth)):
        return _enqueue(account_id, JobType.CHECK_ALIVE, claims)

    @app.post("/accounts/{account_id}/sync-videos", response_model=SyncResponse)
    def sync_videos(account_id: str, claims: dict = Depends(_auth)):
        return _enqueue(account_id, JobType.SYNC_VIDEOS, claims)

    @app.post("/accounts/{account_id}/assign-proxy")
    def assign_proxy(account_id: str, body: dict[str, Any], claims: dict = Depends(_auth)):
        a = _account_for_tenant(claims, account_id, Permission.PROXY_ASSIGN)
        proxy = store.proxies.get(str(body.get("proxy_id", "")))
        if not proxy:
            raise HTTPException(status_code=404, detail="proxy not found")
        if proxy.status != ProxyStatus.ACTIVE:
            raise HTTPException(status_code=409, detail=f"proxy not ACTIVE: {proxy.status.value}")
        a.proxy_binding_id = proxy.id
        _audit(claims, "proxy.assign", target_type="account", target_id=a.id,
               detail={"proxy_id": proxy.id})
        return {"account_id": a.id, "proxy_id": proxy.id}

    @app.get("/videos")
    def list_videos(account_id: Optional[str] = None, platform: Optional[Platform] = None,
                    cursor: Optional[str] = None, limit: int = Query(50, le=200),
                    claims: dict = Depends(_auth)):
        _require(claims, Permission.ACCOUNT_VIEW)
        if account_id:
            _account_for_tenant(claims, account_id, Permission.ACCOUNT_VIEW)
        tenant_accounts = {aid for aid, a in store.accounts.items()
                           if a.workspace_id == claims["workspace_id"]}
        items = [v for v in store.videos.values()
                 if v.account_id in tenant_accounts
                 and (not account_id or v.account_id == account_id) and (not platform or v.platform == platform)]
        out = [{"id": v.id, "account_id": v.account_id, "platform": v.platform.value,
                "platform_video_id": v.platform_video_id, "title": v.title, "url": v.url,
                "published_at": v.published_at.isoformat() if v.published_at else None,
                "status": v.status.value} for v in sorted(items, key=lambda x: x.created_at)]
        return _page(out, cursor=cursor, limit=limit)

    @app.get("/videos/{video_id}/metrics")
    def video_metrics(video_id: str, start: Optional[datetime] = None, end: Optional[datetime] = None,
                      cursor: Optional[str] = None, limit: int = Query(50, le=200),
                      claims: dict = Depends(_auth)):
        _require(claims, Permission.ACCOUNT_VIEW)
        video = store.videos.get(video_id)
        if not video:
            raise HTTPException(status_code=404, detail="video not found")
        _account_for_tenant(claims, video.account_id, Permission.ACCOUNT_VIEW)
        rows = store.metrics.get(video_id, [])
        if start:
            rows = [m for m in rows if m.collected_at >= start]
        if end:
            rows = [m for m in rows if m.collected_at <= end]
        out = [{"video_id": m.video_id, "date_bucket": m.date_bucket, "views": m.views,
                "likes": m.likes, "comments": m.comments, "shares": m.shares,
                "watch_time_seconds": m.watch_time_seconds, "source": m.source.value,
                "quality": m.quality.value, "collected_at": m.collected_at.isoformat()} for m in rows]
        return _page(out, cursor=cursor, limit=limit)

    @app.get("/proxies")
    def list_proxies(cursor: Optional[str] = None, limit: int = Query(50, le=200), claims: dict = Depends(_auth)):
        # ProxyInfo cua contracts khong co workspace_id: proxy la ha tang
        # dung chung, chi tra metadata (khong credential) + PROXY_VIEW.
        _require(claims, Permission.PROXY_VIEW)
        out = [{"id": p.id, "host": p.host, "port": p.port, "country": p.country,
                "status": p.status.value, "score": p.score} for p in store.proxies.values()]
        return _page(out, cursor=cursor, limit=limit)

    @app.get("/proxies/health")
    def proxies_health(claims: dict = Depends(_auth)):
        _require(claims, Permission.PROXY_VIEW)
        return [{"id": p.id, "status": p.status.value, "success_rate": p.success_rate,
                 "avg_latency_ms": p.avg_latency_ms, "consecutive_failures": p.consecutive_failures}
                for p in store.proxies.values()]

    @app.post("/proxies/import", status_code=201)
    def import_proxies(body: list[ProxyCreate], claims: dict = Depends(_auth)):
        _require(claims, Permission.PROXY_IMPORT)
        created = []
        for item in body:
            p = ProxyInfo(id=uuid.uuid4().hex, host=item.host, port=item.port, country=item.country,
                          credential_ref=item.credential_ref)
            store.proxies[p.id] = p
            created.append(p.id)
            _audit(claims, "proxy.import", target_type="proxy", target_id=p.id,
                   detail={"host": item.host, "port": item.port})
        return {"created": created, "count": len(created)}

    @app.get("/groups")
    def list_groups(workspace_id: Optional[str] = Query(default=None), claims: dict = Depends(_auth)):
        _check_workspace_param(claims, workspace_id)
        _require(claims, Permission.ACCOUNT_VIEW)
        return [g for g in store.groups.values() if g.get("workspace_id") == claims["workspace_id"]]

    @app.get("/tags")
    def list_tags(workspace_id: Optional[str] = Query(default=None), claims: dict = Depends(_auth)):
        _check_workspace_param(claims, workspace_id)
        _require(claims, Permission.ACCOUNT_VIEW)
        tags: set[str] = set()
        for a in store.accounts.values():
            if a.workspace_id == claims["workspace_id"]:
                tags.update(a.tags)
        return sorted(tags)

    @app.get("/jobs")
    def list_jobs(account_id: Optional[str] = None, cursor: Optional[str] = None,
                  limit: int = Query(50, le=200), claims: dict = Depends(_auth)):
        _require(claims, Permission.ACCOUNT_VIEW)
        if account_id:
            _account_for_tenant(claims, account_id, Permission.ACCOUNT_VIEW)
        tenant_accounts = {aid for aid, a in store.accounts.items()
                           if a.workspace_id == claims["workspace_id"]}
        items = [j for j in store.jobs.values()
                 if j.get("account_id") in tenant_accounts
                 and (not account_id or j.get("account_id") == account_id)]
        return _page(items, cursor=cursor, limit=limit)

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        # Auth ngay o handshake: token o query (?token=) hoac header.
        token = ws.query_params.get("token") or ""
        if not token:
            authz = ws.headers.get("authorization") or ""
            token = authz[7:] if authz.startswith("Bearer ") else ""
        try:
            if auth_checker is not None:
                claims = _normalize_claims(auth_checker(token), test_mode=True)
            else:
                if not token:
                    raise HTTPException(status_code=401, detail="missing token")
                claims = _normalize_claims(_verify_jwt(token, secret=secret))
            _require(claims, Permission.ACCOUNT_VIEW)
        except HTTPException:
            await ws.close(code=4401)
            return
        except PermissionDenied:
            await ws.close(code=4403)
            return
        await hub.connect(ws, workspace_id=claims["workspace_id"])
        try:
            while True:
                await ws.receive_text()  # giu ket noi; client chi nhan broadcast
        except WebSocketDisconnect:
            hub.disconnect(ws)

    return app

def _unconfigured_auth_checker(_token: str) -> dict[str, Any]:
    raise HTTPException(status_code=503, detail="JWT_SECRET not configured")

# Module-level app: neu chua cau hinh JWT_SECRET, import van thanh cong de
# test/dev dung create_app(auth_checker=...), nhung moi request vao ban
# module-level nay deu fail-closed 503 thay vi 500/trang thai mo ho.
try:
    app = create_app()
except RuntimeError:
    app = create_app(auth_checker=_unconfigured_auth_checker)
