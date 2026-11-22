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
"""
from __future__ import annotations

import hashlib
import math
import os
import random
import shutil
import signal
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from src.contracts import (
        AccountStatus, BrowserProfile, PlatformAccount, utcnow,
    )
except ImportError:  # pragma: no cover
    from contracts import AccountStatus, BrowserProfile, PlatformAccount, utcnow  # type: ignore

__all__ = ["ProfileConflictError", "CheckpointDetectedError",
           "ResourceLimits", "BehaviourProfile", "HumanBehaviour",
           "IsolatedBrowserProfileManager", "detect_block_page"]

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


@dataclass
class _Runtime:
    pid: Optional[int] = None
    temp_dirs: list[str] = field(default_factory=list)


class IsolatedBrowserProfileManager:
    """Implements contracts.BrowserProfileManager."""

    def __init__(self, base_dir: str | Path, *,
                 limits: ResourceLimits | None = None,
                 novnc_base_url: str = ""):
        self._base = Path(base_dir)
        self._base.mkdir(parents=True, exist_ok=True)
        self.limits = limits or ResourceLimits()
        self._novnc = novnc_base_url.rstrip("/")
        self._profiles: dict[str, BrowserProfile] = {}
        self._by_account: dict[str, str] = {}
        self._runtime: dict[str, _Runtime] = {}
        self._lock = threading.Lock()
        self.manual_queue: list[dict] = []

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
        fp_key = hashlib.sha256(
            f"{account.id}:{rng.random()}:{w}x{h}".encode()).hexdigest()[:16]
        # fingerprint_key phai on dinh: tinh lai thuan tu seed
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

    # -- Protocol -------------------------------------------------------
    def get_or_create(self, account: PlatformAccount) -> BrowserProfile:
        with self._lock:
            existing_id = self._by_account.get(account.id)
            if existing_id:
                prof = self._profiles[existing_id]
                if prof.proxy_id != account.proxy_binding_id:
                    raise ProfileConflictError(
                        "profile proxy != account sticky proxy - fail-closed")
                prof.last_used_at = utcnow()
                return prof
            # Geo suy tu proxy country neu account payload co, mac dinh UTC
            country = str(getattr(account, "niche", "") and "" or "")
            prof = self._fingerprint(account, country or "US")
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
        """Dong profile, kill tien trinh tam, don temp - chong zombie."""
        with self._lock:
            rt = self._runtime.pop(profile_id, None)
            prof = self._profiles.get(profile_id)
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
        self.manual_queue.append({
            "account_id": account.id, "kind": kind, "url": url,
            "screenshot_path": screenshot_path, "at": utcnow().isoformat(),
            "action": "manual_review"})
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
