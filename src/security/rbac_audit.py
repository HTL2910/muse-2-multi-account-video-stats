"""approach_16 — RBAC, operator auth (password + TOTP 2FA) and immutable audit.

Contract: uses ``Role`` / ``AuditLogEntry`` / ``Workspace`` from contracts.py.
Stdlib only in this reference impl.:
- Passwords: argon2id/bcrypt if installed, else PBKDF2-HMAC-SHA256 (600k iter)
  with per-user salt — hash string is self-describing so the backend can be
  swapped without a data migration.
- TOTP: RFC 6238 (HMAC-SHA1, 30s step, +/-1 window), mandatory for ADMIN.
- Audit: append-only list/DB table + SHA-256 hash-chain; verify_chain()
  detects any edit/deletion. Secrets are NEVER written to detail (redacted).
  The chain is TAMPER-EVIDENT, not tamper-proof: it detects edits made by
  anyone who bypasses this API, it does not physically prevent them.
  Thread-safety: AuditChain serializes appends with a lock (production:
  one DB transaction + advisory lock per workspace) and never hands out
  references to its stored entries — append()/entries() return deep
  copies. At the PostgreSQL layer, append-only is enforced by the
  trigger in AUDIT_APPEND_ONLY_SQL (no UPDATE/DELETE on the audit table).
- Sessions: opaque token, idle+absolute timeout, revocable. Brute-force:
  per-account+IP token bucket with lockout. Security headers / CORS / CSRF
  helpers are framework-agnostic dicts for the API gateway (approach_13).
"""
from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import secrets
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

from src.contracts import AuditLogEntry, Role, utcnow

__all__ = ["Permission", "ROLE_PERMISSIONS", "has_permission", "require_permission",
           "PermissionDenied", "hash_password", "verify_password", "generate_totp_secret",
           "verify_totp", "AuditChain", "redact_detail", "SENSITIVE_ACTIONS",
           "AUDIT_APPEND_ONLY_SQL",
           "SessionManager", "OperatorSession", "LoginRateLimiter",
           "security_headers", "csrf_token_for", "check_csrf"]


class Permission(str, Enum):
    ACCOUNT_VIEW = "account.view"
    ACCOUNT_CREATE = "account.create"
    ACCOUNT_DELETE = "account.delete"
    ACCOUNT_SYNC = "account.sync"
    PROXY_VIEW = "proxy.view"
    PROXY_ASSIGN = "proxy.assign"
    PROXY_IMPORT = "proxy.import"
    CREDENTIAL_VIEW = "credential.view"      # metadata only, never plaintext
    DATA_EXPORT = "data.export"
    USER_MANAGE = "user.manage"
    AUDIT_VIEW = "audit.view"
    SETTINGS_MANAGE = "settings.manage"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset({Permission.ACCOUNT_VIEW, Permission.PROXY_VIEW}),
    Role.OPERATOR: frozenset({Permission.ACCOUNT_VIEW, Permission.PROXY_VIEW,
                              Permission.ACCOUNT_SYNC}),
    Role.MANAGER: frozenset({Permission.ACCOUNT_VIEW, Permission.PROXY_VIEW,
                             Permission.ACCOUNT_SYNC, Permission.ACCOUNT_CREATE,
                             Permission.PROXY_ASSIGN, Permission.PROXY_IMPORT,
                             Permission.DATA_EXPORT, Permission.AUDIT_VIEW}),
    Role.ADMIN: frozenset(Permission),  # all
}
# Actions that MUST be audited (approach_16 spec)
SENSITIVE_ACTIONS = frozenset({
    "credential.view", "proxy.assign", "proxy.import", "account.delete",
    "account.create", "data.export", "user.manage", "settings.manage",
    "auth.login", "auth.login_failed", "auth.2fa_failed",
})
_SECRET_KEYS = ("password", "token", "cookie", "secret", "totp", "authorization")


class PermissionDenied(PermissionError):
    pass


def has_permission(role: Role, perm: Permission) -> bool:
    return perm in ROLE_PERMISSIONS.get(role, frozenset())


def require_permission(role: Role, perm: Permission, *, workspace_id: str,
                       resource_workspace_id: str) -> None:
    """RBAC + Row-Level Security: tenant may only touch its own workspace."""
    if resource_workspace_id != workspace_id:
        raise PermissionDenied("cross-workspace access denied (RLS)")
    if not has_permission(role, perm):
        raise PermissionDenied(f"role {role.value} lacks {perm.value}")


# --- passwords ---------------------------------------------------------------
def hash_password(password: str) -> str:
    try:  # preferred: argon2id when the optional dependency is present
        from argon2 import PasswordHasher  # type: ignore
        return "argon2id$" + PasswordHasher().hash(password)
    except Exception:
        salt = secrets.token_bytes(16)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
        return "pbkdf2_sha256$600000$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        if stored.startswith("argon2id$"):
            from argon2 import PasswordHasher  # type: ignore
            PasswordHasher().verify(stored[len("argon2id$"):], password)
            return True
        if stored.startswith("pbkdf2_sha256$"):
            _, iters, salt_b64, dk_b64 = stored.split("$")
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                     base64.b64decode(salt_b64), int(iters))
            return hmac.compare_digest(base64.b64encode(dk).decode(), dk_b64)
    except Exception:
        return False
    return False


# --- TOTP (RFC 6238) -----------------------------------------------------------
def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


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


def requires_2fa(role: Role) -> bool:
    return role == Role.ADMIN


# --- audit hash-chain ------------------------------------------------------------
def redact_detail(detail: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***" if any(s in k.lower() for s in _SECRET_KEYS) else v)
            for k, v in detail.items()}


def _entry_hash(entry: AuditLogEntry) -> str:
    payload = json.dumps({
        "prev": entry.prev_hash, "id": entry.id, "ws": entry.workspace_id,
        "actor": entry.actor_id, "action": entry.action,
        "target": f"{entry.target_type}:{entry.target_id}",
        "detail": entry.detail, "ts": entry.created_at.isoformat(),
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()


# Production enforcement of append-only at the PostgreSQL layer (the
# in-process chain below is tamper-EVIDENT; this trigger makes the stored
# table tamper-RESISTANT for anyone going through SQL). Apply once when
# provisioning the audit table; inserts stay allowed, updates/deletes
# raise. Per-workspace serialization in production: wrap the INSERT in a
# transaction that first takes pg_advisory_xact_lock(hashtext(workspace_id))
# and reads MAX(created_at)/last entry_hash inside that same transaction.
AUDIT_APPEND_ONLY_SQL = """
CREATE OR REPLACE FUNCTION audit_log_forbid_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only: % is forbidden', TG_OP;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;
CREATE TRIGGER audit_log_append_only
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_forbid_mutation();
"""


class AuditChain:
    """Append-only, TAMPER-EVIDENT audit log (hash-chain).

    Guarantees provided by THIS class:
    - appends are serialized with a lock, so concurrent writers can never
      fork the chain (two entries sharing one prev_hash);
    - no update/delete API exists, and no method hands out a reference to
      a stored entry: append() and entries() return deep copies, so
      callers cannot mutate history through a returned object;
    - verify_chain() detects any edit/deletion performed by bypassing
      this API (direct memory or DB access) — detection, not prevention.
    Prevention at rest is the database's job: see AUDIT_APPEND_ONLY_SQL.
    """

    def __init__(self) -> None:
        self._entries: list[AuditLogEntry] = []
        self._lock = threading.Lock()

    def append(self, *, workspace_id: str, actor_id: str, action: str,
               target_type: str, target_id: str,
               detail: Optional[dict[str, Any]] = None) -> AuditLogEntry:
        # The read of prev_hash, the hash computation and the append are
        # ONE critical section: without the lock, two concurrent writers
        # could both read the same last entry and fork the chain.
        with self._lock:
            entry = AuditLogEntry(
                id=uuid.uuid4().hex, workspace_id=workspace_id, actor_id=actor_id,
                action=action, target_type=target_type, target_id=target_id,
                detail=redact_detail(detail or {}),
                prev_hash=self._entries[-1].entry_hash if self._entries else "GENESIS")
            entry.entry_hash = _entry_hash(entry)
            self._entries.append(entry)
            return copy.deepcopy(entry)

    def entries(self, *, workspace_id: Optional[str] = None) -> list[AuditLogEntry]:
        """Deep copies of the stored entries — mutating them changes nothing."""
        with self._lock:
            return [copy.deepcopy(e) for e in self._entries
                    if workspace_id is None or e.workspace_id == workspace_id]

    def verify_chain(self) -> bool:
        with self._lock:
            snapshot = list(self._entries)
        prev = "GENESIS"
        for e in snapshot:
            if e.prev_hash != prev or e.entry_hash != _entry_hash(e):
                return False
            prev = e.entry_hash
        return True


# --- sessions & brute-force -------------------------------------------------------
@dataclass
class OperatorSession:
    token: str
    user_id: str
    workspace_id: str
    role: Role
    created_at: datetime = field(default_factory=utcnow)
    last_seen_at: datetime = field(default_factory=utcnow)
    revoked: bool = False


class SessionManager:
    def __init__(self, *, idle_minutes: int = 30, absolute_hours: int = 12):
        self._idle = timedelta(minutes=idle_minutes)
        self._absolute = timedelta(hours=absolute_hours)
        self._sessions: dict[str, OperatorSession] = {}

    def create(self, *, user_id: str, workspace_id: str, role: Role) -> OperatorSession:
        s = OperatorSession(token=secrets.token_urlsafe(32), user_id=user_id,
                            workspace_id=workspace_id, role=role)
        self._sessions[s.token] = s
        return s

    def validate(self, token: str) -> Optional[OperatorSession]:
        s = self._sessions.get(token)
        if s is None or s.revoked:
            return None
        now = utcnow()
        if now - s.last_seen_at > self._idle or now - s.created_at > self._absolute:
            s.revoked = True
            return None
        s.last_seen_at = now
        return s

    def revoke(self, token: str) -> None:
        if token in self._sessions:
            self._sessions[token].revoked = True

    def revoke_all_for_user(self, user_id: str) -> int:
        n = 0
        for s in self._sessions.values():
            if s.user_id == user_id and not s.revoked:
                s.revoked = True
                n += 1
        return n


class LoginRateLimiter:
    """Lock an (account, ip) pair after ``max_failures`` inside ``window``."""

    def __init__(self, *, max_failures: int = 5, window_seconds: int = 900,
                 lock_seconds: int = 900):
        self._max, self._window, self._lock = max_failures, window_seconds, lock_seconds
        self._fails: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    @staticmethod
    def _key(account: str, ip: str) -> str:
        return f"{account.lower()}|{ip}"

    def is_locked(self, account: str, ip: str, *, at: Optional[float] = None) -> bool:
        return self._locked_until.get(self._key(account, ip), 0) > (at or time.time())

    def record_failure(self, account: str, ip: str, *, at: Optional[float] = None) -> bool:
        now = at or time.time()
        key = self._key(account, ip)
        recent = [t for t in self._fails.get(key, []) if now - t < self._window]
        recent.append(now)
        self._fails[key] = recent
        if len(recent) >= self._max:
            self._locked_until[key] = now + self._lock
            return True
        return False

    def record_success(self, account: str, ip: str) -> None:
        self._fails.pop(self._key(account, ip), None)


# --- gateway helpers ----------------------------------------------------------------
def security_headers() -> dict[str, str]:
    return {"Strict-Transport-Security": "max-age=31536000; includeSubDomains",
            "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
            "Cache-Control": "no-store"}


def csrf_token_for(session_token: str, *, secret: str) -> str:
    return hmac.new(secret.encode(), session_token.encode(), hashlib.sha256).hexdigest()


def check_csrf(session_token: str, presented: str, *, secret: str) -> bool:
    return hmac.compare_digest(csrf_token_for(session_token, secret=secret), presented or "")
