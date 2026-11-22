"""approach_17 — Observability, cong fail-closed va kiem thu golden.

Lop observability doc lap voi pipeline chinh: khong lam pipeline chet
khi monitor loi, khong bao gio log/tra secret plaintext.
- Prometheus metrics (dinh dang exposition, khong can client lib)
- OpenTelemetry-style trace context (trace_id/span, account_id/job_id)
- Structured logging da redact secret/PII
- P0 batch gate: schema, views khong giam bat thuong, so video khop
  ky vong, proxy + account deu ACTIVE; fail -> REJECTED/NEEDS_REVIEW,
  khong ghi de du lieu cu
- Fail-closed gate: proxy chet / session het han / loi xac thuc ->
  job REJECTED + audit, khong dung proxy/session mac dinh account khac
- Golden test: fixture co dinh, CI phat hien API doi schema
- Kill-switch: dung toan bo crawl khi ti le loi vuot nguong
Chi dung thu vien chuan.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Optional

try:
    from src.contracts import (  # type: ignore
        RISK_STOP_THRESHOLD, AccountStatus, Alert, AlertSeverity, AlertType,
        BatchDecision, DataQuality, GoldenCase, JobStatus, Platform,
        PlatformAccount, ProxyInfo, ProxyStatus, UnifiedVideoMetric,
        is_fail_closed_status, utcnow, validate_metric,
    )
except ImportError:
    from ..contracts import (  # type: ignore
        RISK_STOP_THRESHOLD, AccountStatus, Alert, AlertSeverity, AlertType,
        BatchDecision, DataQuality, GoldenCase, JobStatus, Platform,
        PlatformAccount, ProxyInfo, ProxyStatus, UnifiedVideoMetric,
        is_fail_closed_status, utcnow, validate_metric,
    )

__all__ = [
    "redact", "new_trace_id", "Monitor", "BatchGateResult",
    "validate_batch", "fail_closed_gate", "KillSwitch",
    "GoldenRunner", "InMemoryAlertNotifier",
]

# Pattern redact: token/secret/password/cookie — thay bang ***.
_SECRET_RE = re.compile(
    r"(?i)(access_token|refresh_token|password|passwd|secret|cookie|"
    r"authorization|api[_-]?key|sessionid|fb_exchange_token)\s*[:=]\s*"
    r"([\"']?)([^\s\"',}]+)\2"
)
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._\-]{6,}")


def redact(text: str) -> str:
    """Che secret/PII trong log; khong bao gio tra secret ve."""
    if not text:
        return text
    text = _SECRET_RE.sub(lambda m: f"{m.group(1)}=***", text)
    return _BEARER_RE.sub(r"\1***", text)


def new_trace_id() -> str:
    return uuid.uuid4().hex


@dataclass
class BatchGateResult:
    decision: BatchDecision
    errors: list[str] = field(default_factory=list)
    checked: int = 0


def validate_batch(metrics: list[UnifiedVideoMetric], *,
                   expected_video_count: Optional[int] = None,
                   previous: Optional[dict[str, UnifiedVideoMetric]] = None,
                   max_views_drop_ratio: float = 0.5) -> BatchGateResult:
    """Kiem tra P0 cho 1 batch thu thap. Fail -> REJECTED/NEEDS_REVIEW."""
    errors: list[str] = []
    for m in metrics:
        for e in validate_metric(m):
            errors.append(f"{m.platform_video_id}: {e}")
    if expected_video_count is not None and len(metrics) != expected_video_count:
        errors.append(f"video count mismatch: got {len(metrics)}, expected {expected_video_count}")
    if previous:
        for m in metrics:
            prev = previous.get(m.video_id)
            if prev and prev.views > 0 and m.views < prev.views * (1 - max_views_drop_ratio):
                errors.append(f"{m.platform_video_id}: views dropped {prev.views} -> {m.views}")
    if errors:
        # Loi schema / count mismatch = REJECTED; chi drop view nhe = NEEDS_REVIEW.
        decision = BatchDecision.NEEDS_REVIEW if all("dropped" in e for e in errors) else BatchDecision.REJECT
        return BatchGateResult(decision=decision, errors=errors, checked=len(metrics))
    return BatchGateResult(decision=BatchDecision.ACCEPT, checked=len(metrics))


def fail_closed_gate(account: PlatformAccount, proxy: Optional[ProxyInfo]) -> tuple[bool, str]:
    """Gate truoc moi job. (True, '') = duoc chay; (False, ly_do) = REJECTED."""
    if is_fail_closed_status(account.status):
        return False, f"account {account.id} status {account.status.value} is fail-closed"
    if proxy is None:
        return False, "no proxy bound (fail-closed: no default proxy)"
    if proxy.status != ProxyStatus.ACTIVE:
        return False, f"proxy {proxy.id} status {proxy.status.value} is not ACTIVE"
    return True, ""


class KillSwitch:
    """Dung toan bo crawl khi ti le loi vuot nguong trong cua so truot."""

    def __init__(self, *, error_rate_threshold: float = RISK_STOP_THRESHOLD,
                 window: int = 100, min_samples: int = 20):
        self.threshold = error_rate_threshold
        self.window = window
        self.min_samples = min_samples
        self._results: deque[bool] = deque(maxlen=window)
        self.tripped = False
        self.tripped_at: Optional[datetime] = None

    def record(self, ok: bool) -> bool:
        self._results.append(ok)
        if len(self._results) >= self.min_samples:
            rate = 1 - sum(self._results) / len(self._results)
            if rate >= self.threshold:
                self.tripped = True
                self.tripped_at = utcnow()
        return self.tripped

    @property
    def error_rate(self) -> float:
        if not self._results:
            return 0.0
        return 1 - sum(self._results) / len(self._results)

    def reset(self) -> None:
        self._results.clear()
        self.tripped = False
        self.tripped_at = None


class InMemoryAlertNotifier:
    """AlertNotifier protocol (in-memory; production gan Telegram/Webhook)."""

    def __init__(self) -> None:
        self.sent: list[Alert] = []

    def notify(self, alert: Alert) -> None:
        self.sent.append(alert)


class Monitor:
    """Observability hub: metrics, trace, log redact, alert, kill-switch."""

    def __init__(self, *, notifier: Optional[InMemoryAlertNotifier] = None,
                 logger: Optional[logging.Logger] = None):
        self.notifier = notifier or InMemoryAlertNotifier()
        self.logger = logger or logging.getLogger("multi_account_video_stats")
        self._counters: Counter = Counter()
        self._gauges: dict[str, float] = {}
        self.kill_switch = KillSwitch()
        self._error_window: deque[bool] = deque(maxlen=100)

    # -- metrics (Prometheus exposition, khong can client lib) ------------
    def inc(self, name: str, *, labels: Optional[dict[str, str]] = None, value: int = 1) -> None:
        key = name + ("" if not labels else "{" + ",".join(f'{k}="{v}"' for k, v in sorted(labels.items())) + "}")
        self._counters[key] += value

    def set_gauge(self, name: str, value: float) -> None:
        self._gauges[name] = value

    def prometheus_text(self) -> str:
        lines = []
        for k, v in sorted(self._counters.items()):
            name = k.split("{")[0]
            lines.append(f"# TYPE {name} counter\n{k} {v}")
        for k, v in sorted(self._gauges.items()):
            lines.append(f"# TYPE {k} gauge\n{k} {v}")
        return "\n".join(lines) + ("\n" if lines else "")

    # -- logging / trace ----------------------------------------------------
    def log(self, level: int, message: str, *, trace_id: Optional[str] = None,
            account_id: str = "", job_id: str = "") -> None:
        safe = redact(message)
        extra = f" trace_id={trace_id or new_trace_id()} account_id={account_id} job_id={job_id}"
        self.logger.log(level, safe + extra)

    def record_job(self, *, ok: bool, platform: Platform, job_status: JobStatus) -> None:
        self.inc("jobs_total", labels={"platform": platform.value, "status": job_status.value})
        self._error_window.append(ok)
        self.kill_switch.record(ok)

    def alert(self, alert: Alert) -> None:
        self.inc("alerts_total", labels={"type": alert.alert_type.value, "severity": alert.severity.value})
        self.notifier.notify(alert)
        self.log(logging.WARNING if alert.severity != AlertSeverity.CRITICAL else logging.ERROR,
                 f"alert {alert.alert_type.value}: {alert.title} — {alert.message}",
                 account_id=alert.account_id or "")

    def check_batch_and_alert(self, metrics: list[UnifiedVideoMetric], *,
                              account: PlatformAccount, proxy: Optional[ProxyInfo],
                              expected_video_count: Optional[int] = None) -> BatchGateResult:
        ok, reason = fail_closed_gate(account, proxy)
        if not ok:
            self.alert(Alert(id=uuid.uuid4().hex, workspace_id=account.workspace_id,
                             alert_type=AlertType.SYNC_FAILED, severity=AlertSeverity.CRITICAL,
                             account_id=account.id, title="fail-closed gate rejected job", message=reason))
            return BatchGateResult(decision=BatchDecision.REJECT, errors=[reason])
        result = validate_batch(metrics, expected_video_count=expected_video_count)
        if result.decision is not BatchDecision.ACCEPT:
            self.alert(Alert(id=uuid.uuid4().hex, workspace_id=account.workspace_id,
                             alert_type=AlertType.SYNC_FAILED, severity=AlertSeverity.WARNING,
                             account_id=account.id, title="batch gate failed",
                             message="; ".join(result.errors)))
        return result


class GoldenRunner:
    """Chay GoldenCase co dinh: adapter map fixture -> metric ky vong.

    ``adapter_map``: (platform, raw_fixture) -> UnifiedVideoMetric.
    CI fail ngay khi API doi schema (count/metric lech).
    """

    def __init__(self, *, load_fixture: Callable[[str], Any],
                 adapter_map: Callable[[Platform, Any], UnifiedVideoMetric]):
        self._load = load_fixture
        self._map = adapter_map

    def run(self, case: GoldenCase) -> tuple[bool, list[str]]:
        errors: list[str] = []
        try:
            raw = self._load(case.raw_fixture_path)
        except Exception as exc:
            return False, [f"cannot load fixture {case.raw_fixture_path}: {exc}"]
        items = raw if isinstance(raw, list) else raw.get("data", [])
        if len(items) != case.expected_video_count:
            errors.append(f"video count {len(items)} != expected {case.expected_video_count}")
        if case.expected_metric is not None and items:
            try:
                got = self._map(case.platform, items[0])
            except Exception as exc:
                return False, [f"adapter mapping failed: {exc}"]
            exp = case.expected_metric
            for f in ("views", "likes", "comments", "shares"):
                if getattr(got, f) != getattr(exp, f):
                    errors.append(f"{f} {getattr(got, f)} != expected {getattr(exp, f)}")
            if got.platform_video_id != exp.platform_video_id:
                errors.append("platform_video_id mismatch")
        return (not errors), errors
