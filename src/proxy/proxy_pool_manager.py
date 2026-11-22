"""approach_04 — Proxy pool manager: sticky 1-1, scoring, health-check, failover.

Contract: implements ``ProxyPool`` protocol from src/contracts.py
(acquire / release / report_result / health_check / failover).
Fail-closed: no healthy proxy in the account's geo -> raise ProxyUnavailable,
NEVER fall back to another account's proxy or a direct connection.
Stdlib only. Persistence is behind ``ProxyRepository`` (in-memory default);
production swaps it for PostgreSQL without changing callers.

Fix ``fix_proxy_pool_fail_closed``:
1. Unverified khong duoc cap: ``health_check()`` khong co checker truoc
   day tra ok=False nhung khong doi status nen proxy van ACTIVE va van
   duoc cap. Tu ban nay health_check khong co checker (hoac checker tra
   ve bad: not ok / ip_leak / dns_leak / blacklisted) se QUARANTINE
   proxy ngay lap tuc (khong doi 3 lan loi) va loai khoi _candidates
   cho toi khi duoc kiem chung that. Production bat buoc co checker:
   ``require_health_checker=True`` thi __init__ raise neu thieu checker;
   ``require_verified=True`` thi chi cap proxy da health-check ok it
   nhat 1 lan.
2. Quarantine co hoi phuc: het ``circuit_open_until`` thi probe lai
   bang health_checker; probe ok -> ACTIVE tro lai, account nao nhan
   proxy do se o probation (WARMUP) theo BINDING/account, khong dat
   WARMUP len ProxyInfo dung chung nua. Probe that bai -> quarantine
   lai voi backoff tang dan. Khong co checker thi khong bao gio tu
   phuc hoi.
3. Failover cung geo that: ``_candidates(country)`` truoc day tra toan
   bo pool khi het proxy cung geo, nen failover len sang quoc gia khac
   va ghi sai reason='failover_same_geo'. Tu ban nay neu co country thi
   chi tra proxy cung geo (co the rong -> ProxyUnavailable); failover
   khong dat WARMUP len ProxyInfo dung chung, ma danh dau probation
   tren binding cua account do.
4. Cap phat doc quyen mac dinh: ``_pick``/``acquire`` loai proxy da
   co binding active cua account khac. Muon share phai khai bao ro
   ``max_accounts_per_proxy`` theo kind (mac dinh 1 cho residential/
   mobile/datacenter — Team Lead da chot); ``accounts_sharing()`` hien
   so account dang share, ``effective_proxy_concurrency()`` ha xuong 1
   khi proxy da share.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional, Protocol

from src.contracts import (
    PROXY_QUARANTINE_FAILURES, ProxyBinding, ProxyHealth, ProxyInfo,
    ProxyKind, ProxyStatus, ProxyType, utcnow,
)

__all__ = ["ProxyUnavailable", "ProxyLease", "ScoreWeights", "parse_proxy_line",
           "parse_proxy_bulk", "ProxyPoolManager", "InMemoryProxyPoolManager"]


class ProxyUnavailable(RuntimeError):
    """Fail-closed signal: no usable proxy for this account/geo."""


@dataclass(frozen=True)
class ScoreWeights:
    # score = w1*success + w2*(1/latency_norm) + w3*stability - w4*ban - w5*cost
    success: float = 0.40
    latency: float = 0.20
    stability: float = 0.20
    ban: float = 0.15
    cost: float = 0.05


@dataclass
class ProxyLease:
    account_id: str
    proxy_id: str
    leased_at: datetime = field(default_factory=utcnow)
    expires_at: Optional[datetime] = None
    last_heartbeat: datetime = field(default_factory=utcnow)

    def is_expired(self, *, at: Optional[datetime] = None) -> bool:
        return self.expires_at is not None and (at or utcnow()) >= self.expires_at


@dataclass
class _Stats:
    requests: int = 0
    successes: int = 0
    bans: int = 0
    cost_per_gb: float = 0.0
    stability: float = 1.0  # 1 - variance proxy, updated by health-checks
    circuit_open_until: Optional[datetime] = None
    backoff_level: int = 0


# host:port | host:port:user:pass | scheme://user:pass@host:port
_LINE_RE = re.compile(
    r"^(?:(?P<scheme>https?|socks5)://)?"
    r"(?:(?P<user>[^:@/]+)(?::(?P<password>[^@/]+))?@)?"
    r"(?P<host>[^:/\s]+):(?P<port>\d{1,5})"
    r"(?:[:](?P<user2>[^:\s]+)[:](?P<password2>[^\s]+))?$"
)


def parse_proxy_line(line: str, *, kind: ProxyKind = ProxyKind.RESIDENTIAL,
                     country: str = "", isp: str = "") -> tuple[ProxyInfo, Optional[str]]:
    """Parse one line -> (ProxyInfo, plaintext_password_or_None).

    The password is returned ONLY so the caller can put it in SecretVault;
    ProxyInfo itself stores credential_ref, never the password.
    """
    m = _LINE_RE.match(line.strip())
    if not m:
        raise ValueError(f"invalid proxy line: {line[:32]!r}...")
    port = int(m.group("port"))
    if not 1 <= port <= 65535:
        raise ValueError(f"invalid port {port}")
    scheme = (m.group("scheme") or "http").lower()
    password = m.group("password") or m.group("password2")
    username = m.group("user") or m.group("user2")
    info = ProxyInfo(id=uuid.uuid4().hex, host=m.group("host"), port=port,
                     proxy_type=ProxyType(scheme), kind=kind, country=country,
                     isp=isp, username=username)
    return info, password


def parse_proxy_bulk(text: str, **kw) -> tuple[list[tuple[ProxyInfo, Optional[str]]], list[str]]:
    ok, errors = [], []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.strip().startswith("#"):
            continue
        try:
            ok.append(parse_proxy_line(line, **kw))
        except ValueError as exc:
            errors.append(f"line {i}: {exc}")
    return ok, errors


def _ring_hash(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest()[:16], 16)


class ProxyPoolManager:
    """Sticky 1-1 pool with EWMA scoring, quarantine, circuit breaker, lease."""

    def __init__(self, *, weights: ScoreWeights = ScoreWeights(),
                 alpha: float = 0.2, epsilon: float = 0.05,
                 lease_ttl_seconds: int = 900,
                 health_checker: Optional[Callable[[ProxyInfo], ProxyHealth]] = None,
                 rng: Optional[random.Random] = None,
                 vault_store: Optional[Callable[..., str]] = None,
                 max_accounts_per_proxy: Optional[dict[ProxyKind, int]] = None,
                 require_health_checker: bool = False,
                 require_verified: bool = False,
                 probation_successes: int = 2):
        if require_health_checker and health_checker is None:
            raise ValueError(
                "health_checker is required (require_health_checker=True): "
                "a proxy pool without a checker cannot verify proxies")
        self._w, self._alpha, self._epsilon = weights, alpha, epsilon
        self._lease_ttl = lease_ttl_seconds
        self._health_checker = health_checker
        self._rng = rng or random.Random()
        self._vault_store = vault_store  # SecretVault.store-compatible
        self._require_verified = require_verified
        self._probation_successes = max(1, probation_successes)
        # Doc quyen mac dinh: 1 account / proxy cho moi kind (Team Lead chot
        # 1 cho residential/mobile; datacenter cung mac dinh 1 neu khong
        # khai bao ro). Muon share thi truyen dict lon hon, vd
        # {ProxyKind.DATACENTER: 5}.
        self._max_per_proxy: dict[ProxyKind, int] = {
            ProxyKind.RESIDENTIAL: 1, ProxyKind.MOBILE: 1, ProxyKind.DATACENTER: 1,
        }
        if max_accounts_per_proxy:
            self._max_per_proxy.update(max_accounts_per_proxy)
        self._proxies: dict[str, ProxyInfo] = {}
        self._stats: dict[str, _Stats] = {}
        self._bindings: dict[str, ProxyBinding] = {}       # account_id -> active binding
        self._history: list[ProxyBinding] = []
        self._leases: dict[str, ProxyLease] = {}
        self._health: dict[str, list[ProxyHealth]] = {}
        self._verified: set[str] = set()      # proxy_id da health-check ok it nhat 1 lan
        self._unverified: set[str] = set()    # proxy_id bi health-check bad / khong co checker
        self._reactivated: set[str] = set()   # proxy_id vua phuc hoi tu quarantine
        self._probation: dict[str, str] = {}  # account_id -> proxy_id dang probation (per-binding)
        self._probation_ok: dict[str, int] = {}  # account_id -> so request ok trong probation

    # --- import / registration -------------------------------------------
    def add_proxy(self, proxy: ProxyInfo, *, password: Optional[str] = None) -> ProxyInfo:
        if password and self._vault_store:
            proxy.credential_ref = self._vault_store(
                account_id=proxy.id, kind="proxy_password", secret=password)
        self._proxies[proxy.id] = proxy
        self._stats.setdefault(proxy.id, _Stats())
        return proxy

    def import_bulk(self, text: str, **kw) -> tuple[int, list[str]]:
        parsed, errors = parse_proxy_bulk(text, **kw)
        for info, pw in parsed:
            self.add_proxy(info, password=pw)
        return len(parsed), errors

    def get(self, proxy_id: str) -> Optional[ProxyInfo]:
        return self._proxies.get(proxy_id)

    def bindings_history(self, account_id: str) -> list[ProxyBinding]:
        return [b for b in self._history if b.account_id == account_id]

    # --- sharing / probation (hien thi) ------------------------------------
    def sharing_count(self, proxy_id: str) -> int:
        """So account dang co binding active toi proxy nay."""
        return sum(1 for b in self._bindings.values() if b.proxy_id == proxy_id)

    def accounts_sharing(self, proxy_id: str) -> list[str]:
        """Danh sach account_id dang share proxy nay (doc quyen -> toi da 1)."""
        return [b.account_id for b in self._bindings.values() if b.proxy_id == proxy_id]

    def effective_proxy_concurrency(self, proxy_id: str, *, default: int = 2) -> int:
        """Concurrency thuc cua proxy: ha xuong 1 khi proxy da share
        (>= 2 account), neu khong giu nguyen default."""
        return 1 if self.sharing_count(proxy_id) >= 2 else default

    def is_probation(self, account_id: str) -> bool:
        """Account co dang o WARMUP probation (per-binding) khong?"""
        return account_id in self._probation

    def probation_proxy(self, account_id: str) -> Optional[str]:
        return self._probation.get(account_id)

    # --- scoring -----------------------------------------------------------
    def compute_score(self, proxy: ProxyInfo) -> float:
        st = self._stats[proxy.id]
        latency_norm = min(1.0, proxy.avg_latency_ms / 2000.0) if proxy.avg_latency_ms else 0.5
        ban_rate = (st.bans / st.requests) if st.requests else 0.0
        cost_norm = min(1.0, st.cost_per_gb / 15.0)
        raw = (self._w.success * proxy.success_rate
               + self._w.latency * (1.0 - latency_norm)
               + self._w.stability * st.stability
               - self._w.ban * ban_rate
               - self._w.cost * cost_norm)
        return round(max(0.0, min(1.0, raw)), 6)

    def report_result(self, *, proxy_id: str, ok: bool, latency_ms: float,
                      banned: bool = False, account_id: Optional[str] = None) -> None:
        proxy = self._proxies.get(proxy_id)
        if proxy is None:
            return
        st = self._stats[proxy_id]
        st.requests += 1
        a = self._alpha
        proxy.success_rate = (1 - a) * proxy.success_rate + a * (1.0 if ok else 0.0)
        if latency_ms > 0:
            proxy.avg_latency_ms = ((1 - a) * proxy.avg_latency_ms + a * latency_ms
                                    if proxy.avg_latency_ms else float(latency_ms))
        if banned:
            st.bans += 1
        if ok:
            st.successes += 1
            proxy.consecutive_failures = 0
            st.backoff_level = 0
            st.circuit_open_until = None
            # Trang thai WARMUP khong nam tren ProxyInfo dung chung nua:
            # probation nam tren binding/account (xem _probation). Mot
            # request ok khong tu dong nang WARMUP -> ACTIVE cho moi
            # account khac dang dung chung proxy do.
            self._note_probation_success(proxy_id, account_id)
        else:
            proxy.consecutive_failures += 1
            if proxy.consecutive_failures >= PROXY_QUARANTINE_FAILURES:
                self._quarantine(proxy)
        proxy.score = self.compute_score(proxy)
        proxy.last_check_at = utcnow()

    def _note_probation_success(self, proxy_id: str, account_id: Optional[str]) -> None:
        """Dem request ok trong probation; du so lan thi het probation
        cho account do (khong anh huong account khac)."""
        if account_id is not None:
            targets = [account_id] if self._probation.get(account_id) == proxy_id else []
        else:
            targets = [a for a, p in self._probation.items() if p == proxy_id]
        for acc in targets:
            self._probation_ok[acc] = self._probation_ok.get(acc, 0) + 1
            if self._probation_ok[acc] >= self._probation_successes:
                del self._probation[acc]
                self._probation_ok.pop(acc, None)
                if not any(p == proxy_id for p in self._probation.values()):
                    self._reactivated.discard(proxy_id)

    def _mark_probation(self, account_id: str, proxy_id: str) -> None:
        """Danh dau binding cua account dang o WARMUP probation."""
        self._probation[account_id] = proxy_id
        self._probation_ok[account_id] = 0

    def _quarantine(self, proxy: ProxyInfo) -> None:
        st = self._stats[proxy.id]
        proxy.status = ProxyStatus.QUARANTINED
        self._verified.discard(proxy.id)
        self._unverified.add(proxy.id)
        st.backoff_level += 1
        # exponential backoff: 60s, 120s, 240s, ... capped at 1h
        delay = min(3600, 60 * (2 ** (st.backoff_level - 1)))
        st.circuit_open_until = utcnow() + timedelta(seconds=delay)

    def _circuit_open(self, proxy: ProxyInfo) -> bool:
        until = self._stats[proxy.id].circuit_open_until
        return until is not None and utcnow() < until

    def _usable(self, proxy: ProxyInfo) -> bool:
        if proxy.status not in (ProxyStatus.ACTIVE, ProxyStatus.WARMUP):
            return False
        if self._circuit_open(proxy):
            return False
        if proxy.id in self._unverified:
            return False
        if self._require_verified and proxy.id not in self._verified:
            return False
        return True

    # --- reactivation --------------------------------------------------------
    def _maybe_reactivate(self, proxy: ProxyInfo) -> bool:
        """Het circuit thi probe lai 1 lan. Probe ok -> ACTIVE + danh
        dau reactivated (account nhan proxy se vao probation); probe
        that bai -> quarantine lai voi backoff tang. Khong co checker
        thi khong bao gio tu phuc hoi."""
        if proxy.status != ProxyStatus.QUARANTINED:
            return False
        if self._circuit_open(proxy):
            return False
        if self._health_checker is None:
            return False
        health = self.health_check(proxy.id)
        return health.ok and self._usable(proxy)

    def reactivate_due_proxies(self) -> list[str]:
        """Probe tat ca proxy quarantined da het circuit. Tra ve id
        cac proxy vua phuc hoi. Dinh ky goi tu reaper job."""
        recovered: list[str] = []
        for proxy in list(self._proxies.values()):
            if self._maybe_reactivate(proxy):
                recovered.append(proxy.id)
        return recovered

    # --- sticky acquire / lease -------------------------------------------
    def _candidates(self, country: str = "", *, for_account_id: Optional[str] = None) -> list[ProxyInfo]:
        # Co hoi phuc hoi truoc khi chon: proxy quarantined het circuit
        # duoc probe 1 lan o day (that bai thi circuit mo lai, khong
        # probe lien tuc).
        for proxy in list(self._proxies.values()):
            if proxy.status == ProxyStatus.QUARANTINED and not self._circuit_open(proxy):
                self._maybe_reactivate(proxy)
        pool = [p for p in self._proxies.values() if self._usable(p)]
        if country:
            # Cung geo that: het proxy cung geo thi tra [] (fail-closed),
            # KHONG tra toan bo pool sang quoc gia khac.
            pool = [p for p in pool if p.country.upper() == country.upper()]
        if for_account_id is not None:
            # Doc quyen mac dinh: loai proxy da co binding cua account khac
            # toi gioi han share cua kind do.
            pool = [p for p in pool
                    if p.id == (self._bindings.get(for_account_id).proxy_id
                                if self._bindings.get(for_account_id) else None)
                    or self.sharing_count(p.id) < self._max_per_proxy.get(p.kind, 1)]
        return pool

    def _pick(self, account_id: str, country: str = "") -> ProxyInfo:
        pool = self._candidates(country, for_account_id=account_id)
        if not pool:
            raise ProxyUnavailable(f"no usable proxy (geo={country or 'any'})")
        # epsilon-greedy: occasionally trial the least-used healthy proxy
        if self._rng.random() < self._epsilon and len(pool) > 1:
            return min(pool, key=lambda p: self._stats[p.id].requests)
        # consistent hashing account_id -> ring position, then best score near it
        target = _ring_hash(account_id)
        pool.sort(key=lambda p: (abs(_ring_hash(p.id) - target), -p.score))
        best_score = max(p.score for p in pool[:3])
        return next(p for p in pool[:3] if p.score == best_score)

    def _bind(self, account_id: str, proxy: ProxyInfo, reason: str) -> ProxyBinding:
        old = self._bindings.get(account_id)
        if old and old.proxy_id != proxy.id:
            old.unbound_at = utcnow()
        binding = ProxyBinding(id=uuid.uuid4().hex, account_id=account_id,
                               proxy_id=proxy.id, reason=reason)
        self._bindings[account_id] = binding
        self._history.append(binding)
        if proxy.id in self._reactivated:
            self._mark_probation(account_id, proxy.id)
        return binding

    def acquire(self, account_id: str) -> ProxyInfo:
        binding = self._bindings.get(account_id)
        if binding:
            current = self._proxies.get(binding.proxy_id)
            if current and self._usable(current):
                self._renew_lease(account_id, current.id)
                return current
            if current and self._maybe_reactivate(current):
                # Proxy cu cua account vua phuc hoi: giu sticky, vao probation.
                self._mark_probation(account_id, current.id)
                self._renew_lease(account_id, current.id)
                return current
            # sticky proxy died -> controlled failover, same geo only
            return self.failover(account_id)
        proxy = self._pick(account_id)
        self._bind(account_id, proxy, reason="initial_sticky_assign")
        self._renew_lease(account_id, proxy.id)
        return proxy

    def _renew_lease(self, account_id: str, proxy_id: str) -> ProxyLease:
        lease = ProxyLease(account_id=account_id, proxy_id=proxy_id,
                           expires_at=utcnow() + timedelta(seconds=self._lease_ttl))
        self._leases[account_id] = lease
        return lease

    def heartbeat(self, account_id: str) -> bool:
        lease = self._leases.get(account_id)
        if lease is None or lease.is_expired():
            return False
        lease.last_heartbeat = utcnow()
        lease.expires_at = utcnow() + timedelta(seconds=self._lease_ttl)
        return True

    def release(self, account_id: str) -> None:
        # Lease released; the sticky BINDING is kept (1-1) for the next job.
        self._leases.pop(account_id, None)

    def failover(self, account_id: str) -> ProxyInfo:
        binding = self._bindings.get(account_id)
        old = self._proxies.get(binding.proxy_id) if binding else None
        country = old.country if old else ""
        pool = [p for p in self._candidates(country, for_account_id=account_id)
                if not old or p.id != old.id]
        if not pool:
            raise ProxyUnavailable(
                f"failover: no same-geo ({country}) replacement for account {account_id}")
        replacement = max(pool, key=lambda p: p.score)
        # KHONG dat WARMUP len ProxyInfo dung chung nua (se ha trang
        # thai cac account khac). Account nay vao probation tren
        # binding cua rieng no.
        self._bind(account_id, replacement,
                   reason="failover_same_geo" if country else "failover")
        self._mark_probation(account_id, replacement.id)
        self._renew_lease(account_id, replacement.id)
        return replacement

    # --- health-check -------------------------------------------------------
    def health_check(self, proxy_id: str) -> ProxyHealth:
        proxy = self._proxies.get(proxy_id)
        if proxy is None:
            return ProxyHealth(proxy_id=proxy_id, ok=False, error="proxy_not_found")
        if self._health_checker is None:
            # Khong co checker thi khong the kiem chung: fail-closed —
            # quarantine ngay, loai khoi candidates cho toi khi duoc
            # kiem chung that (khong de ACTIVE nhu truoc).
            health = ProxyHealth(proxy_id=proxy_id, ok=False,
                                 error="no_health_checker_configured")
            self._health.setdefault(proxy_id, []).append(health)
            self._quarantine(proxy)
            proxy.last_check_at = health.checked_at
            return health
        circuit_was_open = self._circuit_open(proxy)
        health = self._health_checker(proxy)
        self._health.setdefault(proxy_id, []).append(health)
        bad = (not health.ok or health.ip_leak or health.dns_leak or health.blacklisted)
        self.report_result(proxy_id=proxy_id, ok=not bad,
                           latency_ms=health.latency_ms, banned=health.blacklisted)
        if bad:
            # Tin hieu xau dinh danh (leak/blacklist/that bai kiem tra):
            # quarantine NGAY, khong doi 3 lan loi nhu loi request thuong.
            if proxy.status != ProxyStatus.QUARANTINED:
                self._quarantine(proxy)
            else:
                self._unverified.add(proxy.id)
        else:
            self._verified.add(proxy.id)
            self._unverified.discard(proxy.id)
            if proxy.status == ProxyStatus.QUARANTINED and not circuit_was_open:
                # Het circuit + probe ok -> phuc hoi, account nhan proxy
                # se vao probation (per-binding, xem _reactivated).
                proxy.status = ProxyStatus.ACTIVE
                self._reactivated.add(proxy.id)
        proxy.last_check_at = health.checked_at
        return health

    def health_check_all(self) -> list[ProxyHealth]:
        return [self.health_check(pid) for pid in list(self._proxies)]


# Backwards-friendly alias (approach name in registry / docs)
InMemoryProxyPoolManager = ProxyPoolManager
