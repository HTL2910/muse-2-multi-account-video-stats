"""approach_02 — Unified account model + lifecycle state machine.

Stdlib only. Imports only the locked contract (src/contracts.py v1.0.0).
PostgreSQL is the source of truth; this module is the in-memory /
validation core that the DB layer must mirror. Raw payloads are never
stored here — see POSTGRES_DDL (raw_payload table has a TTL).

Fail-closed: an illegal transition is rejected and audited, never
silently coerced; fail-closed statuses (contracts.FAIL_CLOSED_STATUSES)
mean no automatic job may run for that account.

Import rule (fix_import_dual_contracts): exactly ONE import style for
the contract — ``from src.contracts import ...`` run from the repo root.
No ``from contracts import`` / ``from ..contracts import`` fallback:
a fallback can load the same file as a second, distinct module object
(``contracts`` vs ``src.contracts``), so Enum/dataclass identity checks
(``is``, ``isinstance``) silently break across modules.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Optional

from src.contracts import (
    ALLOWED_ACCOUNT_TRANSITIONS,
    FAIL_CLOSED_STATUSES,
    AccountStatus,
    AuditLogEntry,
    AuthMethod,
    Platform,
    PlatformAccount,
    TokenSet,
    is_fail_closed_status,
    utcnow,
)

__all__ = [
    "POSTGRES_DDL", "InvalidTransitionError", "ValidationError",
    "AuditLog", "AccountRepository", "natural_key", "validate_account",
    "transition", "create_account", "check_token_lifecycle",
    "assign_proxy", "import_accounts_csv", "export_accounts_csv",
    "import_account_rows", "mask_credential_ref",
]

POSTGRES_DDL = """
-- accounts: source of truth. Natural key is UNIQUE per platform.
CREATE TABLE IF NOT EXISTS accounts (
  id UUID PRIMARY KEY,
  workspace_id UUID NOT NULL,
  platform TEXT NOT NULL CHECK (platform IN ('facebook','tiktok','youtube')),
  platform_user_id TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  avatar_url TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'created',
  auth_method TEXT NOT NULL DEFAULT 'oauth2',
  credential_ref TEXT NOT NULL DEFAULT '',
  proxy_binding_id UUID,
  profile_id UUID,
  owner TEXT NOT NULL DEFAULT '',
  niche TEXT NOT NULL DEFAULT '',
  tags TEXT[] NOT NULL DEFAULT '{}',
  group_ids UUID[] NOT NULL DEFAULT '{}',
  health_score DOUBLE PRECISION NOT NULL DEFAULT 1.0 CHECK (health_score BETWEEN 0 AND 1),
  risk_level TEXT NOT NULL DEFAULT 'low',
  last_check_at TIMESTAMPTZ, last_sync_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (platform, platform_user_id)
);
CREATE INDEX IF NOT EXISTS idx_accounts_ws_status ON accounts (workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts (owner);
-- raw payloads: separate table, TTL, never joined into normalised reads.
CREATE TABLE IF NOT EXISTS raw_payloads (
  id UUID PRIMARY KEY, account_id UUID NOT NULL REFERENCES accounts(id),
  platform_video_id TEXT, payload JSONB NOT NULL, collected_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at TIMESTAMPTZ NOT NULL DEFAULT (now() + interval '30 days')
);
CREATE INDEX IF NOT EXISTS idx_raw_expires ON raw_payloads (expires_at);
-- audit log: append-only, hash-chained (enforced by app + trigger in prod).
CREATE TABLE IF NOT EXISTS audit_log (
  id UUID PRIMARY KEY, workspace_id UUID NOT NULL, actor_id TEXT NOT NULL,
  action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}', prev_hash TEXT NOT NULL DEFAULT '',
  entry_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


class InvalidTransitionError(ValueError):
    pass


class ValidationError(ValueError):
    pass


def mask_credential_ref(ref: str) -> str:
    """Never log a secret; a credential_ref is only a pointer, mask it anyway."""
    if not ref:
        return ""
    return ref[:4] + "***" if len(ref) > 4 else "***"


def natural_key(platform: Platform, platform_user_id: str) -> str:
    """Idempotent natural key: (platform, platform_user_id), normalised."""
    if not platform_user_id or not str(platform_user_id).strip():
        raise ValidationError("platform_user_id is required")
    return f"{platform.value}:{str(platform_user_id).strip()}"


def validate_account(a: PlatformAccount) -> list[str]:
    errors: list[str] = []
    if not a.id:
        errors.append("missing id")
    if not a.workspace_id:
        errors.append("missing workspace_id")
    if not a.platform_user_id or not a.platform_user_id.strip():
        errors.append("missing platform_user_id")
    if not 0.0 <= a.health_score <= 1.0:
        errors.append("health_score out of range [0, 1]")
    if a.proxy_binding_id is not None and not a.proxy_binding_id:
        errors.append("empty proxy_binding_id")
    return errors


def _audit_hash(prev: str, e: AuditLogEntry) -> str:
    payload = "|".join([
        prev, e.workspace_id, e.actor_id, e.action, e.target_type, e.target_id,
        json.dumps(e.detail, sort_keys=True, default=str),
        e.created_at.isoformat(),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class AuditLog:
    """Append-only audit log with a hash chain. No update/delete API."""

    entries: list[AuditLogEntry] = field(default_factory=list)

    def append(self, *, workspace_id: str, actor_id: str, action: str,
               target_type: str, target_id: str,
               detail: Optional[dict[str, Any]] = None) -> AuditLogEntry:
        prev = self.entries[-1].entry_hash if self.entries else ""
        entry = AuditLogEntry(
            id=uuid.uuid4().hex, workspace_id=workspace_id, actor_id=actor_id,
            action=action, target_type=target_type, target_id=target_id,
            detail=dict(detail or {}), prev_hash=prev,
        )
        entry.entry_hash = _audit_hash(prev, entry)
        self.entries.append(entry)
        return entry

    def verify(self) -> list[str]:
        """Return list of broken entry ids (empty = chain intact)."""
        broken: list[str] = []
        prev = ""
        for e in self.entries:
            if e.prev_hash != prev or e.entry_hash != _audit_hash(prev, e):
                broken.append(e.id)
            prev = e.entry_hash
        return broken


def transition(account: PlatformAccount, to: AccountStatus, *,
               actor_id: str, audit: Optional[AuditLog] = None,
               reason: str = "") -> PlatformAccount:
    """The only way to change status. Illegal transitions raise and are audited."""
    if to == account.status:
        raise InvalidTransitionError(f"no-op transition to {to.value}")
    allowed = ALLOWED_ACCOUNT_TRANSITIONS.get(account.status, frozenset())
    if to not in allowed:
        if audit is not None:
            audit.append(workspace_id=account.workspace_id, actor_id=actor_id,
                         action="account.transition_rejected", target_type="account",
                         target_id=account.id,
                         detail={"from": account.status.value, "to": to.value, "reason": reason})
        raise InvalidTransitionError(
            f"illegal transition {account.status.value} -> {to.value}")
    old = account.status
    account.status = to
    account.updated_at = utcnow()
    if audit is not None:
        audit.append(workspace_id=account.workspace_id, actor_id=actor_id,
                     action="account.transition", target_type="account",
                     target_id=account.id,
                     detail={"from": old.value, "to": to.value, "reason": reason})
    return account


def create_account(*, workspace_id: str, platform: Platform,
                   platform_user_id: str, owner: str = "", niche: str = "",
                   display_name: str = "", auth_method: AuthMethod = AuthMethod.OAUTH2,
                   credential_ref: str = "", tags: Optional[list[str]] = None,
                   group_ids: Optional[list[str]] = None) -> PlatformAccount:
    a = PlatformAccount(
        id=uuid.uuid4().hex, workspace_id=workspace_id, platform=platform,
        platform_user_id=str(platform_user_id).strip(), display_name=display_name,
        status=AccountStatus.CREATED, auth_method=auth_method,
        credential_ref=credential_ref, owner=owner, niche=niche,
        tags=list(tags or []), group_ids=list(group_ids or []),
    )
    errs = validate_account(a)
    if errs:
        raise ValidationError("; ".join(errs))
    return a


def check_token_lifecycle(account: PlatformAccount, tokens: Optional[TokenSet],
                          *, actor_id: str = "system",
                          audit: Optional[AuditLog] = None) -> AccountStatus:
    """Map token state onto the state machine. Never refreshes here (approach_03/06 own that)."""
    if tokens is None:
        return account.status
    if tokens.is_expired():
        if account.status in (AccountStatus.ACTIVE, AccountStatus.TOKEN_EXPIRING):
            transition(account, AccountStatus.TOKEN_EXPIRED, actor_id=actor_id, audit=audit, reason="token expired")
    elif tokens.needs_refresh():
        if account.status == AccountStatus.ACTIVE:
            transition(account, AccountStatus.TOKEN_EXPIRING, actor_id=actor_id, audit=audit, reason="token expires soon")
    return account.status


def assign_proxy(account: PlatformAccount, proxy_id: str, *,
                 actor_id: str, audit: Optional[AuditLog] = None) -> PlatformAccount:
    """Sticky 1-1 binding. Re-binding is allowed; it is audited, never silent."""
    old = account.proxy_binding_id
    account.proxy_binding_id = proxy_id
    account.updated_at = utcnow()
    if audit is not None:
        audit.append(workspace_id=account.workspace_id, actor_id=actor_id,
                     action="proxy.assign", target_type="account", target_id=account.id,
                     detail={"from": old, "to": proxy_id})
    return account


@dataclass
class UpsertResult:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)


class AccountRepository:
    """In-memory mirror of the PostgreSQL table (same uniqueness rules)."""

    def __init__(self, audit: Optional[AuditLog] = None):
        self._by_id: dict[str, PlatformAccount] = {}
        self._by_natural: dict[str, str] = {}
        self.audit = audit or AuditLog()

    def upsert(self, account: PlatformAccount, *, actor_id: str = "system") -> tuple[PlatformAccount, bool]:
        errs = validate_account(account)
        if errs:
            raise ValidationError("; ".join(errs))
        key = natural_key(account.platform, account.platform_user_id)
        existing_id = self._by_natural.get(key)
        if existing_id and existing_id != account.id:
            # Idempotent upsert: keep the original internal id, update fields.
            existing = self._by_id[existing_id]
            for f in ("display_name", "avatar_url", "owner", "niche", "tags", "group_ids",
                      "proxy_binding_id", "profile_id", "health_score", "risk_level"):
                setattr(existing, f, getattr(account, f))
            existing.updated_at = utcnow()
            self.audit.append(workspace_id=existing.workspace_id, actor_id=actor_id,
                              action="account.upsert", target_type="account", target_id=existing.id,
                              detail={"natural_key": key, "created": False})
            return existing, False
        self._by_id[account.id] = account
        self._by_natural[key] = account.id
        self.audit.append(workspace_id=account.workspace_id, actor_id=actor_id,
                          action="account.upsert", target_type="account", target_id=account.id,
                          detail={"natural_key": key, "created": True})
        return account, True

    def get(self, account_id: str) -> Optional[PlatformAccount]:
        return self._by_id.get(account_id)

    def get_by_natural(self, platform: Platform, platform_user_id: str) -> Optional[PlatformAccount]:
        aid = self._by_natural.get(natural_key(platform, platform_user_id))
        return self._by_id.get(aid) if aid else None

    def list(self, *, workspace_id: Optional[str] = None, platform: Optional[Platform] = None,
             status: Optional[AccountStatus] = None, owner: Optional[str] = None,
             niche: Optional[str] = None, tag: Optional[str] = None,
             group_id: Optional[str] = None, query: str = "") -> list[PlatformAccount]:
        out = list(self._by_id.values())
        if workspace_id: out = [a for a in out if a.workspace_id == workspace_id]
        if platform: out = [a for a in out if a.platform == platform]
        if status: out = [a for a in out if a.status == status]
        if owner: out = [a for a in out if a.owner == owner]
        if niche: out = [a for a in out if a.niche == niche]
        if tag: out = [a for a in out if tag in a.tags]
        if group_id: out = [a for a in out if group_id in a.group_ids]
        if query:
            q = query.lower()
            out = [a for a in out if q in a.display_name.lower() or q in a.platform_user_id.lower()]
        return sorted(out, key=lambda a: a.created_at)

    def fail_closed_accounts(self) -> list[PlatformAccount]:
        return [a for a in self._by_id.values() if is_fail_closed_status(a.status)]


_CSV_FIELDS = ["platform", "platform_user_id", "display_name", "owner", "niche",
               "auth_method", "credential_ref", "tags", "group_ids"]


def import_account_rows(rows: Iterable[dict[str, Any]], *, workspace_id: str,
                        repo: AccountRepository, actor_id: str = "import") -> UpsertResult:
    result = UpsertResult()
    for i, row in enumerate(rows):
        try:
            platform = Platform(str(row.get("platform", "")).strip().lower())
            account = create_account(
                workspace_id=workspace_id, platform=platform,
                platform_user_id=str(row.get("platform_user_id", "")),
                display_name=str(row.get("display_name", "") or ""),
                owner=str(row.get("owner", "") or ""), niche=str(row.get("niche", "") or ""),
                auth_method=AuthMethod(str(row.get("auth_method", "oauth2")).strip().lower()),
                credential_ref=str(row.get("credential_ref", "") or ""),
                tags=[t.strip() for t in str(row.get("tags", "") or "").split(";") if t.strip()],
                group_ids=[t.strip() for t in str(row.get("group_ids", "") or "").split(";") if t.strip()],
            )
            _, created = repo.upsert(account, actor_id=actor_id)
            result.created += int(created)
            result.updated += int(not created)
        except Exception as exc:  # per-row failure must not abort the batch
            result.errors.append({"row": i, "error": str(exc), "platform_user_id": row.get("platform_user_id")})
    return result


def import_accounts_csv(text: str, *, workspace_id: str, repo: AccountRepository,
                        actor_id: str = "import") -> UpsertResult:
    reader = csv.DictReader(io.StringIO(text))
    return import_account_rows(list(reader), workspace_id=workspace_id, repo=repo, actor_id=actor_id)


def export_accounts_csv(accounts: Iterable[PlatformAccount]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=_CSV_FIELDS + ["id", "status", "health_score"])
    w.writeheader()
    for a in accounts:
        w.writerow({
            "platform": a.platform.value, "platform_user_id": a.platform_user_id,
            "display_name": a.display_name, "owner": a.owner, "niche": a.niche,
            "auth_method": a.auth_method.value, "credential_ref": mask_credential_ref(a.credential_ref),
            "tags": ";".join(a.tags), "group_ids": ";".join(a.group_ids),
            "id": a.id, "status": a.status.value, "health_score": a.health_score,
        })
    return buf.getvalue()
