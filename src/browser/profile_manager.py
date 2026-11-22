"""approach_11 - Co lap browser profile va mo phong hanh vi.

Tuan thu contracts.BrowserProfileManager (get_or_create /
open_for_manual_action / close) va contracts.BrowserProfile.

Nguyen tac:
- 1 account = 1 profile: user_data_dir rieng, fingerprint on dinh suy
  tu hash(account_id) - cung account luon ra cung UA/viewport/WebGL,
  khac account khac fingerprint. Timezone/locale khop geo cua proxy.
- Khong bao gio dung chung profile; proxy cua profile PHAI trung
  proxy sticky cua account (fail-closed neu lech).
- Worker chay gioi han tai nguyen (CPU/RAM/timeout), close() don ca
  tien trinh tam de chong Chromium zombie.
- Hanh vi: watch time log-normal theo do dai video, nghi Poisson,
  moi khoang deu co jitter, thao tac xac suat thap. Chi la tham so
  phan phoi - module nay khong tu like/comment hang loat.
- Checkpoint/captcha: dung job, screenshot, NEEDS_REVIEW/manual queue.
Playwright chi import lazy khi launch that; logic profile/hanh vi
chay duoc bang stdlib de test.

Fix ``fix_browser_profile_geo_runtime`` (gop
fix_browser_profile_geo_always_us x2 + fix_profile_close_loses_runtime):
- Geo: truoc day country tinh bang bieu thuc
  ``str(getattr(account, "niche", "") and "" or "")`` — luon ra ""
  roi roi ve mac dinh "US", nen moi profile deu America/New_York /
  en-US bat ke proxy o dau. Nay country lay tu ProxyInfo cua proxy
  sticky dang bind voi account (tra qua registry ``proxies`` hoac
  ``proxy_resolver`` theo account.proxy_binding_id), KHONG suy tu
  niche. Khong xac dinh duoc country thi dung UTC + en-US kem canh
  bao co cau truc (geo_warnings + log), TUYET DOI khong mac dinh US.
  Profile da tao ma geo cua proxy hien tai lech timezone/locale da
  luu -> ProfileConflictError (fail-closed, chong drift).
- Dong fp_key thua trong _fingerprint da xoa (chi con mot lan tinh
  thuan tu seed).
- close(): truoc day chi pop _runtime, giu _profiles/_by_account
  nen mo lai thi mat theo doi pid/temp, de Chromium zombie va ro ri
  trang thai. Nay close() go profile khoi CA BA dict, kill tien
  trinh + don temp nhu cu; get_or_create o nhanh existing cung tu
  tao lai _Runtime neu thieu. Fingerprint suy thuan tu seed nen
  dong/mo bao nhieu vong profile van giong het.
- manual_queue: truoc day la list RAM, restart la mat het muc
  checkpoint/captcha cho xu ly tay. Nay la SQLiteManualQueue ben
  vung (mac dinh file manual_queue.sqlite3 canh base_dir, hoac
  store duong dan/scheduler queue tiem vao) co trang thai
  PENDING/IN_PROGRESS/DONE/FAILED, dem attempts va retry: fail duoi
  nguong thi quay lai PENDING, qua max_attempts thi FAILED; enqueue
  trung (account, kind) dang cho thi tra ve muc cu, khong nhan doi.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import shutil
import signal
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

try:
    from src.contracts import (
        AccountStatus, BrowserProfile, PlatformAccount, ProxyInfo, utcnow,
    )
except ImportError:  # pragma: no cover
    from contracts import AccountStatus, BrowserProfile, PlatformAccount, ProxyInfo, utcnow  # type: ignore

__all__ = ["ProfileConflictError", "CheckpointDetectedError",
           "ResourceLimits", "BehaviourProfile", "HumanBehaviour",
           "IsolatedBrowserProfileManager", "detect_block_page",
           "ManualQueueStore", "SQLiteManualQueue"]

_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]
_VIEWPORTS = [(1280, 720), (1366, 768), (1440, 900), (1536, 864), (1920, 1080)]
_GEO_LOCALE = {  # country -> (timezone, locale) khop proxy geo
    "VN": ("Asia/Ho_Chi_Minh", "vi-VN"), "US": ("America/New_York", "en-US"),
    "GB": ("Europe/London", "en-GB"), "JP": ("Asia/Tokyo", "ja-JP"),
    "KR": ("Asia/Seoul", "ko-KR"), "DE": ("Europe/Berlin", "de-DE"),
}
_BLOCK_MARKERS = ("captcha", "checkpoint", "verify it's you",
                  "xac minh", "unusual traffic", "are you a robot",
                  "two-factor", "2-step verification")

_log = logging.getLogger("browser.profile_manager")


class ProfileConflictError(RuntimeError):
    pass


class CheckpointDetectedError(RuntimeError):
    """Dung job ngay, chuyen manual queue - khong co gang vuot captcha."""


@dataclass(frozen=True)
class ResourceLimits:
    cpu_seconds: int = 300
    memory_mb: int = 1024
    job_timeout_seconds: int = 600
    max_processes_per_worker: int = 1


@dataclass
class BehaviourProfile:
    watch_ratio_median: float = 0.55   # ti le do dai video xem trung vi
    watch_ratio_sigma: float = 0.45    # do lech log-normal
    rest_lambda_seconds: float = 45.0  # trung binh Poisson khoang nghi
    scroll_probability: float = 0.35
    like_probability: float = 0.03
    comment_probability: float = 0.005
    jitter_ratio: float = 0.25


class HumanBehaviour:
    """Sinh lich hanh vi co phan phoi thong ke, seed on dinh theo phien."""

    def __init__(self, profile: BehaviourProfile | None = None,
                 *, seed: int | None = None):
        self.p = profile or BehaviourProfile()
        self._rng = random.Random(seed)

    def _jitter(self, value: float) -> float:
        j = self.p.jitter_ratio
        return max(0.1, value * self._rng.uniform(1 - j, 1 + j))

    def watch_seconds(self, duration_seconds: float) -> float:
        if duration_seconds <= 0:
            return 0.0
        ratio = self._rng.lognormvariate(
            math.log(self.p.watch_ratio_median), self.p.watch_ratio_sigma)
        ratio = min(1.0, max(0.02, ratio))
        return round(self._jitter(duration_seconds * ratio), 2)

    def rest_seconds(self) -> float:
        # Poisson qua Knuth (lambda vua) - hoac exponential xap xi khi lon
        lam = self.p.rest_lambda_seconds
        if lam > 30:
            return round(self._jitter(self._rng.expovariate(1.0 / lam)), 2)
        limit, k, prod = math.exp(-lam), 0, 1.0
        while True:
            prod *= self._rng.random()
            if prod <= limit:
                return float(k)
            k += 1

    def should(self, action: str) -> bool:
        prob = {"scroll": self.p.scroll_probability,
                "like": self.p.like_probability,
                "comment": self.p.comment_probability}.get(action, 0.0)
        return self._rng.random() < prob

    def plan_session(self, durations: list[float]) -> list[dict]:
        plan = []
        for d in durations:
            plan.append({"watch_seconds": self.watch_seconds(d),
                         "rest_after_seconds": self.rest_seconds(),
                         "scroll": self.should("scroll"),
                         "like": self.should("like")})
        return plan


def detect_block_page(*, url: str = "", text: str = "") -> Optional[str]:
    """Tra loai block neu trang la checkpoint/captcha, else None."""
    hay = f"{url} {text}".lower()
    for marker in _BLOCK_MARKERS:
        if marker in hay:
            return "captcha" if "captcha" in marker or "robot" in marker \
                or "unusual traffic" in marker else "checkpoint"
    return None


# ---------------------------------------------------------------- Manual queue (ben vung)
class ManualQueueStore(Protocol):
    """Hang doi xu ly tay BEN VUNG: muc cho phai song qua restart."""

    def enqueue(self, item: dict) -> str: ...
    def list_items(self, statuses: tuple[str, ...] = ("PENDING", "IN_PROGRESS")) -> list[dict]: ...
    def claim(self, item_id: str) -> Optional[dict]: ...
    def complete(self, item_id: str) -> bool: ...
    def fail(self, item_id: str, error: str = "", *, max_attempts: int = 3) -> Optional[str]: ...


class SQLiteManualQueue:
    """ManualQueueStore bang SQLite (stdlib). Moi thao tac mo mot
    connection ngan va commit ngay — muc cho khong bao gio chi nam
    trong RAM. Trang thai: PENDING -> IN_PROGRESS (claim, attempts+1)
    -> DONE (complete) hoac quay lai PENDING/FAILED (fail theo
    max_attempts). Enqueue trung (account_id, kind) dang cho thi tra
    ve id cu — retry o caller khong nhan doi hang doi."""

    def __init__(self, path: str | Path):
        self._path = str(path)
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS manual_queue ("
                "id TEXT PRIMARY KEY, account_id TEXT NOT NULL, kind TEXT NOT NULL,"
                " url TEXT DEFAULT '', screenshot_path TEXT DEFAULT '',"
                " status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0,"
                " created_at TEXT NOT NULL, updated_at TEXT NOT NULL, last_error TEXT DEFAULT '')")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "account_id": row["account_id"],
                "kind": row["kind"], "url": row["url"],
                "screenshot_path": row["screenshot_path"],
                "status": row["status"], "attempts": row["attempts"],
                "at": row["created_at"], "updated_at": row["updated_at"],
                "last_error": row["last_error"], "action": "manual_review"}

    def enqueue(self, item: dict) -> str:
        now = utcnow().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id FROM manual_queue WHERE account_id=? AND kind=? "
                "AND status IN ('PENDING','IN_PROGRESS') ORDER BY created_at LIMIT 1",
                (item["account_id"], item["kind"])).fetchone()
            if row:
                return row["id"]
            item_id = f"mq-{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO manual_queue (id, account_id, kind, url,"
                " screenshot_path, status, attempts, created_at, updated_at)"
                " VALUES (?,?,?,?,?,'PENDING',0,?,?)",
                (item_id, item["account_id"], item["kind"],
                 item.get("url", ""), item.get("screenshot_path", ""), now, now))
            return item_id

    def list_items(self, statuses: tuple[str, ...] = ("PENDING", "IN_PROGRESS")) -> list[dict]:
        marks = ",".join("?" for _ in statuses)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM manual_queue WHERE status IN ({marks})"
                " ORDER BY created_at", tuple(statuses)).fetchall()
            return [self._row(r) for r in rows]

    def get(self, item_id: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM manual_queue WHERE id=?",
                               (item_id,)).fetchone()
            return self._row(row) if row else None

    def claim(self, item_id: str) -> Optional[dict]:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE manual_queue SET status='IN_PROGRESS',"
                " attempts=attempts+1, updated_at=? WHERE id=? AND status='PENDING'",
                (utcnow().isoformat(), item_id))
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM manual_queue WHERE id=?",
                               (item_id,)).fetchone()
            return self._row(row)

    def complete(self, item_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE manual_queue SET status='DONE', updated_at=? WHERE id=?",
                (utcnow().isoformat(), item_id))
            return cur.rowcount > 0

    def fail(self, item_id: str, error: str = "", *, max_attempts: int = 3) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT attempts FROM manual_queue WHERE id=?",
                               (item_id,)).fetchone()
            if row is None:
                return None
            status = "FAILED" if row["attempts"] >= max_attempts else "PENDING"
            conn.execute(
                "UPDATE manual_queue SET status=?, last_error=?, updated_at=?"
                " WHERE id=?",
                (status, error, utcnow().isoformat(), item_id))
            return status


@dataclass
class _Runtime:
    pid: Optional[int] = None
    temp_dirs: list[str] = field(default_factory=list)


class IsolatedBrowserProfileManager:
    """Implements contracts.BrowserProfileManager.

    ``proxies`` / ``proxy_resolver``: nguon tra ProxyInfo cua proxy
    sticky theo account.proxy_binding_id de lay country that.
    ``manual_queue_store`` / ``manual_queue_path``: hang doi tay ben
    vung; mac dinh SQLite tai <base_dir>/manual_queue.sqlite3.
    """

    def __init__(self, base_dir: str | Path, *,
                 limits: ResourceLimits | None = None,
                 novnc_base_url: str = "",
                 proxies: Optional[dict[str, ProxyInfo]] = None,
                 proxy_resolver: Optional[Callable[[str], Optional[ProxyInfo]]] = None,
                 manual_queue_store: Optional[ManualQueueStore] = None,
                 manual_queue_path: str | Path | None = None):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self.limits = limits or ResourceLimits()
        self._novnc = novnc_base_url.rstrip("/")
        self._proxies: dict[str, ProxyInfo] = dict(proxies or {})
        self._proxy_resolver = proxy_resolver
        self._queue: ManualQueueStore = manual_queue_store or SQLiteManualQueue(
            manual_queue_path or (self._base / "manual_queue.sqlite3"))
        self._profiles: dict[str, BrowserProfile] = {}
        self._by_account: dict[str, str] = {}
        self._runtime: dict[str, _Runtime] = {}
        self._lock = threading.Lock()
        self.geo_warnings: list[dict] = []

    # -- manual queue (ben vung, thay list RAM cu) ----------------------
    @property
    def manual_queue(self) -> list[dict]:
        """Cac muc dang cho xu ly tay (doc tu store ben vung)."""
        return self._queue.list_items()

    def claim_manual_item(self, item_id: str) -> Optional[dict]:
        return self._queue.claim(item_id)

    def complete_manual_item(self, item_id: str) -> bool:
        return self._queue.complete(item_id)

    def fail_manual_item(self, item_id: str, error: str = "") -> Optional[str]:
        return self._queue.fail(item_id, error)

    # -- geo tu proxy sticky ---------------------------------------------
    def _sticky_proxy(self, account: PlatformAccount) -> Optional[ProxyInfo]:
        direct = getattr(account, "proxy", None)
        if isinstance(direct, ProxyInfo):
            return direct
        binding = account.proxy_binding_id
        if not binding:
            return None
        if binding in self._proxies:
            return self._proxies[binding]
        if self._proxy_resolver is not None:
            return self._proxy_resolver(binding)
        return None

    def _resolve_country(self, account: PlatformAccount) -> str:
        """Country cua proxy sticky dang bind. '' neu khong xac dinh
        duoc — caller se dung UTC + canh bao, khong mac dinh US."""
        proxy = self._sticky_proxy(account)
        if proxy is None:
            return ""
        return str(proxy.country or "").strip().upper()

    def _warn_geo(self, account: PlatformAccount, country: str, reason: str) -> None:
        entry = {"account_id": account.id, "country": country,
                 "reason": reason, "fallback": "UTC/en-US",
                 "at": utcnow().isoformat()}
        self.geo_warnings.append(entry)
        _log.warning(json.dumps({"level": "warning", "component": "browser",
                                 "event": "profile_geo_unresolved", **entry},
                                sort_keys=True))

    # -- fingerprint on dinh ------------------------------------------
    @staticmethod
    def _seed(account_id: str) -> int:
        return int(hashlib.sha256(f"fp:{account_id}".encode()).hexdigest()[:8], 16)

    def _fingerprint(self, account: PlatformAccount,
                     country: str) -> BrowserProfile:
        seed = self._seed(account.id)
        rng = random.Random(seed)
        tz, locale = _GEO_LOCALE.get(country.upper(), ("UTC", "en-US"))
        w, h = _VIEWPORTS[rng.randrange(len(_VIEWPORTS))]
        # fingerprint_key on dinh: tinh thuan tu seed (mot lan duy nhat)
        fp_key = hashlib.sha256(
            f"fp:{account.id}:{_UA_POOL[seed % len(_UA_POOL)]}:{w}x{h}".encode()
        ).hexdigest()
        return BrowserProfile(
            id=f"profile-{account.id}", account_id=account.id,
            user_data_dir=str(self._base / account.id),
            user_agent=_UA_POOL[seed % len(_UA_POOL)],
            timezone=tz, locale=locale,
            viewport_width=w, viewport_height=h,
            proxy_id=account.proxy_binding_id, fingerprint_key=fp_key)

    # -- runtime tracking -------------------------------------------------
    def register_runtime(self, profile_id: str, *, pid: Optional[int] = None,
                         temp_dir: Optional[str] = None) -> None:
        """Worker ghi lai pid/temp cua Chromium da launch de close()
        don sach (chong zombie)."""
        with self._lock:
            rt = self._runtime.setdefault(profile_id, _Runtime())
            if pid is not None:
                rt.pid = pid
            if temp_dir is not None:
                rt.temp_dirs.append(temp_dir)

    def runtime_for(self, profile_id: str) -> Optional[_Runtime]:
        with self._lock:
            return self._runtime.get(profile_id)

    # -- Protocol -------------------------------------------------------
    def get_or_create(self, account: PlatformAccount) -> BrowserProfile:
        with self._lock:
            existing_id = self._by_account.get(account.id)
            if existing_id:
                prof = self._profiles[existing_id]
                if prof.proxy_id != account.proxy_binding_id:
                    raise ProfileConflictError(
                        "profile proxy != account sticky proxy - fail-closed")
                # Geo drift: proxy hien tai co country ro rang ma
                # timezone/locale da luu khong khop -> fail-closed,
                # khong am tham chay profile VN voi mui gio JP.
                country = self._resolve_country(account)
                if country in _GEO_LOCALE:
                    tz, locale = _GEO_LOCALE[country]
                    if (prof.timezone, prof.locale) != (tz, locale):
                        raise ProfileConflictError(
                            f"profile geo {prof.timezone}/{prof.locale} != "
                            f"sticky proxy geo {tz}/{locale} - fail-closed")
                # Runtime co the da bi close/pop mat: tao lai de lan
                # mo nay van duoc theo doi pid/temp day du.
                self._runtime.setdefault(existing_id, _Runtime())
                prof.last_used_at = utcnow()
                return prof
            country = self._resolve_country(account)
            if country not in _GEO_LOCALE:
                self._warn_geo(account, country,
                               "no_sticky_proxy_country" if not country
                               else "country_not_in_geo_map")
            prof = self._fingerprint(account, country)
            Path(prof.user_data_dir).mkdir(parents=True, exist_ok=True)
            (Path(prof.user_data_dir) / ".profile-owner").write_text(
                account.id, encoding="utf-8")
            self._profiles[prof.id] = prof
            self._by_account[account.id] = prof.id
            self._runtime[prof.id] = _Runtime()
            return prof

    def validate_owner(self, profile: BrowserProfile, account_id: str) -> bool:
        """Chong dung nham profile cua account khac."""
        marker = Path(profile.user_data_dir) / ".profile-owner"
        return marker.exists() and marker.read_text(encoding="utf-8").strip() == account_id

    def open_for_manual_action(self, account: PlatformAccount) -> str:
        prof = self.get_or_create(account)
        if self._novnc:
            return f"{self._novnc}/vnc.html?path=profile/{prof.id}"
        return f"manual://profile/{prof.id}?dir={prof.user_data_dir}"

    def close(self, profile_id: str) -> None:
        """Dong profile: kill tien trinh, don temp, va GO profile
        khoi ca ba dict theo doi (_profiles/_by_account/_runtime).

        Go han dang ky la an toan vi profile duoc suy lai xac dinh tu
        seed(account_id) o lan get_or_create sau — fingerprint khong
        doi — trong khi giu dang ky cu sau close chi lam mat theo doi
        pid/temp va de Chromium zombie. Idempotent: dong id khong ton
        tai la no-op.
        """
        with self._lock:
            rt = self._runtime.pop(profile_id, None)
            prof = self._profiles.pop(profile_id, None)
            if prof is not None:
                self._by_account.pop(prof.account_id, None)
        if rt and rt.pid:
            try:
                os.killpg(os.getpgid(rt.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    os.kill(rt.pid, signal.SIGTERM)
                except OSError:
                    pass
        if rt:
            for d in rt.temp_dirs:
                shutil.rmtree(d, ignore_errors=True)
        if prof:
            tmp = Path(prof.user_data_dir) / "tmp"
            shutil.rmtree(tmp, ignore_errors=True)

    # -- Job guard --------------------------------------------------------
    def handle_block(self, account: PlatformAccount, *, url: str = "",
                     text: str = "", screenshot_path: str = "") -> Optional[AccountStatus]:
        kind = detect_block_page(url=url, text=text)
        if not kind:
            return None
        self._queue.enqueue({
            "account_id": account.id, "kind": kind, "url": url,
            "screenshot_path": screenshot_path})
        return AccountStatus.CAPTCHA_REQUIRED if kind == "captcha" \
            else AccountStatus.CHECKPOINT

    def launch_args(self, profile: BrowserProfile) -> dict:
        """Cau hinh persistent context (Playwright) - goi lazy o worker."""
        if profile.proxy_id is None:
            raise ProfileConflictError(
                "no sticky proxy bound - fail-closed, refuse to launch")
        return {"user_data_dir": profile.user_data_dir, "headless": True,
                "user_agent": profile.user_agent,
                "timezone_id": profile.timezone, "locale": profile.locale,
                "viewport": {"width": profile.viewport_width,
                             "height": profile.viewport_height},
                "proxy_id": profile.proxy_id,
                "timeout_ms": self.limits.job_timeout_seconds * 1000}
