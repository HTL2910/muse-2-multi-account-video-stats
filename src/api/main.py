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
JWT: kiem tra chu ky bang secret tu env (khong hardcode); test co the
inject ``auth_checker`` de bo qua. Rate-limit theo tenant (in-memory,
production thay bang Redis). WebSocket broadcast su kien
account_status_changed / sync_completed / alert.
OpenAPI/Swagger tu dong tu FastAPI; SDK TypeScript sinh tu OpenAPI.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
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
        AccountStatus, JobPriority, JobStatus, JobType, Platform,
        PlatformAccount, ProxyInfo, ProxyStatus, Video,
        UnifiedVideoMetric, utcnow,
    )
except ImportError:
    from ..contracts import (  # type: ignore
        AccountStatus, JobPriority, JobStatus, JobType, Platform,
        PlatformAccount, ProxyInfo, ProxyStatus, Video,
        UnifiedVideoMetric, utcnow,
    )

__all__ = ["create_app", "app", "EventHub", "InMemoryStore", "RateLimiter",
           "encode_cursor", "decode_cursor"]

# --- Cursor pagination ------------------------------------------------------
def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()

def decode_cursor(cursor: Optional[str]) -> int:
    if not cursor:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(cursor.encode()).decode()).get("o", 0))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid cursor")

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
    """Broadcast WebSocket; moi event co account_id/job_id de trace."""
    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, event: dict[str, Any]) -> None:
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

# --- JWT (HS256, secret tu env; khong hardcode) -----------------------------
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
    workspace_id: str
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

def create_app(*, store: Optional[InMemoryStore] = None,
               scheduler: Any = None,
               auth_checker: Optional[Callable[[str], dict[str, Any]]] = None,
               jwt_secret: Optional[str] = None) -> FastAPI:
    store = store or InMemoryStore()
    hub = EventHub()
    limiter = RateLimiter()
    secret = jwt_secret or os.environ.get("JWT_SECRET", "")

    def _auth(authorization: Optional[str] = Header(default=None)) -> dict[str, Any]:
        if auth_checker is not None:
            return auth_checker(authorization or "")
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        if not secret:
            raise HTTPException(status_code=500, detail="JWT_SECRET not configured")
        return _verify_jwt(authorization[7:], secret=secret)

    def _rate(claims: dict[str, Any]) -> None:
        tenant = str(claims.get("workspace_id") or claims.get("sub") or "default")
        if not limiter.allow(tenant):
            raise HTTPException(status_code=429, detail="rate limited for tenant")

    app = FastAPI(title="multi-account-video-stats API", version="1.0.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    app.state.store = store
    app.state.hub = hub
    app.state.scheduler = scheduler

    def _page(items: list[Any], *, cursor: Optional[str], limit: int):
        off = decode_cursor(cursor)
        page = items[off:off + limit]
        nxt = encode_cursor(off + limit) if off + limit < len(items) else None
        return {"items": page, "next_cursor": nxt, "total": len(items)}

    @app.get("/health")
    def health():
        return {"status": "ok", "contracts": "1.0.0"}

    @app.post("/auth/login")
    def login(body: dict[str, Any]):
        # Demo/dev: khong cap token that khi chua co user store (approach_16).
        # Production: verify argon2id + 2FA o day, roi moi ky JWT.
        raise HTTPException(status_code=501, detail="auth handled by approach_16 (RBAC)")

    @app.post("/accounts", response_model=AccountOut, status_code=201)
    def create_account(body: AccountCreate, claims: dict = Depends(_auth)):
        _rate(claims)
        a = PlatformAccount(id=uuid.uuid4().hex, workspace_id=body.workspace_id,
                            platform=body.platform, platform_user_id=body.platform_user_id,
                            display_name=body.display_name, credential_ref=body.credential_ref,
                            owner=body.owner, niche=body.niche, tags=list(body.tags))
        store.accounts[a.id] = a
        return AccountOut.from_account(a)

    @app.get("/accounts")
    def list_accounts(workspace_id: str = Query(...), platform: Optional[Platform] = None,
                      status: Optional[AccountStatus] = None, search: str = "",
                      cursor: Optional[str] = None, limit: int = Query(50, le=200),
                      claims: dict = Depends(_auth)):
        _rate(claims)
        items = [AccountOut.from_account(a).model_dump(mode="json")
                 for a in store.list_accounts(workspace_id=workspace_id, platform=platform, status=status, search=search)]
        return _page(items, cursor=cursor, limit=limit)

    @app.get("/accounts/{account_id}", response_model=AccountOut)
    def get_account(account_id: str, claims: dict = Depends(_auth)):
        a = store.accounts.get(account_id)
        if not a:
            raise HTTPException(status_code=404, detail="account not found")
        return AccountOut.from_account(a)

    @app.delete("/accounts/{account_id}", status_code=204)
    def delete_account(account_id: str, claims: dict = Depends(_auth)):
        if account_id not in store.accounts:
            raise HTTPException(status_code=404, detail="account not found")
        del store.accounts[account_id]
        return None

    @app.post("/accounts/import", status_code=201)
    def import_accounts(body: list[AccountCreate], claims: dict = Depends(_auth)):
        _rate(claims)
        created = []
        for item in body:
            a = PlatformAccount(id=uuid.uuid4().hex, workspace_id=item.workspace_id,
                                platform=item.platform, platform_user_id=item.platform_user_id,
                                display_name=item.display_name, credential_ref=item.credential_ref)
            store.accounts[a.id] = a
            created.append(a.id)
        return {"created": created, "count": len(created)}

    def _enqueue(account_id: str, job_type: JobType, claims: dict) -> SyncResponse:
        a = store.accounts.get(account_id)
        if not a:
            raise HTTPException(status_code=404, detail="account not found")
        if scheduler is None:
            job_id = uuid.uuid4().hex
            store.jobs[job_id] = {"id": job_id, "job_type": job_type.value, "account_id": account_id, "status": JobStatus.PENDING.value}
            return SyncResponse(account_id=account_id, job_id=job_id, status=JobStatus.PENDING, message="queued (no scheduler injected)")
        try:
            from src.contracts import CollectJob  # type: ignore
        except ImportError:
            from ..contracts import CollectJob  # type: ignore
        job = CollectJob(id=uuid.uuid4().hex, job_type=job_type, account_id=account_id, platform=a.platform)
        scheduler.enqueue(job)
        store.jobs[job.id] = {"id": job.id, "job_type": job_type.value, "account_id": account_id, "status": job.status.value}
        return SyncResponse(account_id=account_id, job_id=job.id, status=job.status, message="queued")

    @app.post("/accounts/{account_id}/check-alive", response_model=SyncResponse)
    def check_alive(account_id: str, claims: dict = Depends(_auth)):
        return _enqueue(account_id, JobType.CHECK_ALIVE, claims)

    @app.post("/accounts/{account_id}/sync-videos", response_model=SyncResponse)
    def sync_videos(account_id: str, claims: dict = Depends(_auth)):
        return _enqueue(account_id, JobType.SYNC_VIDEOS, claims)

    @app.post("/accounts/{account_id}/assign-proxy")
    def assign_proxy(account_id: str, body: dict[str, Any], claims: dict = Depends(_auth)):
        a = store.accounts.get(account_id)
        proxy = store.proxies.get(str(body.get("proxy_id", "")))
        if not a or not proxy:
            raise HTTPException(status_code=404, detail="account or proxy not found")
        if proxy.status != ProxyStatus.ACTIVE:
            raise HTTPException(status_code=409, detail=f"proxy not ACTIVE: {proxy.status.value}")
        a.proxy_binding_id = proxy.id
        return {"account_id": a.id, "proxy_id": proxy.id}

    @app.get("/videos")
    def list_videos(account_id: Optional[str] = None, platform: Optional[Platform] = None,
                    cursor: Optional[str] = None, limit: int = Query(50, le=200),
                    claims: dict = Depends(_auth)):
        items = [v for v in store.videos.values()
                 if (not account_id or v.account_id == account_id) and (not platform or v.platform == platform)]
        out = [{"id": v.id, "account_id": v.account_id, "platform": v.platform.value,
                "platform_video_id": v.platform_video_id, "title": v.title, "url": v.url,
                "published_at": v.published_at.isoformat() if v.published_at else None,
                "status": v.status.value} for v in sorted(items, key=lambda x: x.created_at)]
        return _page(out, cursor=cursor, limit=limit)

    @app.get("/videos/{video_id}/metrics")
    def video_metrics(video_id: str, start: Optional[datetime] = None, end: Optional[datetime] = None,
                      cursor: Optional[str] = None, limit: int = Query(50, le=200),
                      claims: dict = Depends(_auth)):
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
        out = [{"id": p.id, "host": p.host, "port": p.port, "country": p.country,
                "status": p.status.value, "score": p.score} for p in store.proxies.values()]
        return _page(out, cursor=cursor, limit=limit)

    @app.get("/proxies/health")
    def proxies_health(claims: dict = Depends(_auth)):
        return [{"id": p.id, "status": p.status.value, "success_rate": p.success_rate,
                 "avg_latency_ms": p.avg_latency_ms, "consecutive_failures": p.consecutive_failures}
                for p in store.proxies.values()]

    @app.post("/proxies/import", status_code=201)
    def import_proxies(body: list[ProxyCreate], claims: dict = Depends(_auth)):
        created = []
        for item in body:
            p = ProxyInfo(id=uuid.uuid4().hex, host=item.host, port=item.port, country=item.country,
                          credential_ref=item.credential_ref)
            store.proxies[p.id] = p
            created.append(p.id)
        return {"created": created, "count": len(created)}

    @app.get("/groups")
    def list_groups(workspace_id: str = Query(...), claims: dict = Depends(_auth)):
        return [g for g in store.groups.values() if g.get("workspace_id") == workspace_id]

    @app.get("/tags")
    def list_tags(workspace_id: str = Query(...), claims: dict = Depends(_auth)):
        tags: set[str] = set()
        for a in store.accounts.values():
            if a.workspace_id == workspace_id:
                tags.update(a.tags)
        return sorted(tags)

    @app.get("/jobs")
    def list_jobs(account_id: Optional[str] = None, cursor: Optional[str] = None,
                  limit: int = Query(50, le=200), claims: dict = Depends(_auth)):
        items = [j for j in store.jobs.values() if not account_id or j.get("account_id") == account_id]
        return _page(items, cursor=cursor, limit=limit)

    @app.websocket("/ws/events")
    async def ws_events(ws: WebSocket):
        await hub.connect(ws)
        try:
            while True:
                await ws.receive_text()  # giu ket noi; client chi nhan broadcast
        except WebSocketDisconnect:
            hub.disconnect(ws)

    return app

# Module-level app dung JWT that (JWT_SECRET tu env); test inject auth_checker qua create_app().
app = create_app()
