"""approach_04 — Proxy pool manager: sticky 1-1, scoring, health-check, failover.

Contract: implements ``ProxyPool`` protocol from src/contracts.py
(acquire / release / report_result / health_check / failover).
Fail-closed: no healthy proxy in the account's geo -> raise ProxyUnavailable,
NEVER fall back to another account's proxy or a direct connection.
Stdlib only. Persistence is behind ``ProxyRepository`` (in-memory default);
production swaps it for PostgreSQL without changing callers.
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
                 vault_store: Optional[Callable[..., str]] = None):
        self._w, self._alpha, self._epsilon = weights, alpha, epsilon
        self._lease_ttl = lease_ttl_seconds
        self._health_checker = health_checker
        self._rng = rng or random.Random()
        self._vault_store = vault_store  # SecretVault.store-compatible
        self._proxies: dict[str, ProxyInfo] = {}
        self._stats: dict[str, _Stats] = {}
        self._bindings: dict[str, ProxyBinding] = {}       # account_id -> active binding
        self._history: list[ProxyBinding] = []
        self._leases: dict[str, ProxyLease] = {}
        self._health: dict[str, list[ProxyHealth]] = {}

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
                      banned: bool = False) -> None:
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
            if proxy.status == ProxyStatus.WARMUP:
                proxy.status = ProxyStatus.ACTIVE
        else:
            proxy.consecutive_failures += 1
            if proxy.consecutive_failures >= PROXY_QUARANTINE_FAILURES:
                self._quarantine(proxy)
        proxy.score = self.compute_score(proxy)
        proxy.last_check_at = utcnow()

    def _quarantine(self, proxy: ProxyInfo) -> None:
        st = self._stats[proxy.id]
        proxy.status = ProxyStatus.QUARANTINED
        st.backoff_level += 1
        # exponential backoff: 60s, 120s, 240s, ... capped at 1h
        delay = min(3600, 60 * (2 ** (st.backoff_level - 1)))
        st.circuit_open_until = utcnow() + timedelta(seconds=delay)

    def _circuit_open(self, proxy: ProxyInfo) -> bool:
        until = self._stats[proxy.id].circuit_open_until
        return until is not None and utcnow() < until

    def _usable(self, proxy: ProxyInfo) -> bool:
        return (proxy.status in (ProxyStatus.ACTIVE, ProxyStatus.WARMUP)
                and not self._circuit_open(proxy))

    # --- sticky acquire / lease -------------------------------------------
    def _candidates(self, country: str = "") -> list[ProxyInfo]:
        pool = [p for p in self._proxies.values() if self._usable(p)]
        if country:
            same_geo = [p for p in pool if p.country.upper() == country.upper()]
            if same_geo:
                pool = same_geo
        return pool

    def _pick(self, account_id: str, country: str = "") -> ProxyInfo:
        pool = self._candidates(country)
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
        return binding

    def acquire(self, account_id: str) -> ProxyInfo:
        binding = self._bindings.get(account_id)
        if binding:
            current = self._proxies.get(binding.proxy_id)
            if current and self._usable(current):
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
        pool = [p for p in self._candidates(country) if not old or p.id != old.id]
        if not pool:
            raise ProxyUnavailable(
                f"failover: no same-geo ({country}) replacement for account {account_id}")
        replacement = max(pool, key=lambda p: p.score)
        replacement.status = ProxyStatus.WARMUP  # account re-enters warm-up
        self._bind(account_id, replacement, reason="failover_same_geo")
        self._renew_lease(account_id, replacement.id)
        return replacement

    # --- health-check -------------------------------------------------------
    def health_check(self, proxy_id: str) -> ProxyHealth:
        proxy = self._proxies.get(proxy_id)
        if proxy is None:
            return ProxyHealth(proxy_id=proxy_id, ok=False, error="proxy_not_found")
        if self._health_checker is None:
            health = ProxyHealth(proxy_id=proxy_id, ok=False,
                                 error="no_health_checker_configured")
            return health  # fail-closed: unverified != healthy
        health = self._health_checker(proxy)
        self._health.setdefault(proxy_id, []).append(health)
        bad = (not health.ok or health.ip_leak or health.dns_leak or health.blacklisted)
        self.report_result(proxy_id=proxy_id, ok=not bad,
                           latency_ms=health.latency_ms, banned=health.blacklisted)
        if bad and proxy.status != ProxyStatus.QUARANTINED:
            proxy.consecutive_failures = max(proxy.consecutive_failures, 1)
        proxy.last_check_at = health.checked_at
        return health

    def health_check_all(self) -> list[ProxyHealth]:
        return [self.health_check(pid) for pid in list(self._proxies)]


# Backwards-friendly alias (approach name in registry / docs)
InMemoryProxyPoolManager = ProxyPoolManager
