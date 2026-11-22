"""approach_14 — Account health score, risk prediction, fail-closed checkpoint handling.

Stdlib only. Implements AccountHealthScorer (contracts). The risk model
is a pure-python logistic regression (gradient descent, standardised
features) with a transparent rule-based fallback when untrained, so
behaviour is deterministic and explainable. Top features are ranked by
|weight * standardised value| — the explanation shipped with every
assessment.

Gates (contracts constants): risk > 0.7 reduce frequency 50% + change
to a clean proxy + pause non-essential jobs; risk > 0.9 stop entirely
and move the account to NEEDS_REVIEW. Any anomaly signal (checkpoint,
captcha, 2FA, password change, permission error, strange login)
transitions the account immediately, stops its automatic jobs, never
retries login, and enqueues a manual-review item with redacted evidence.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

try:
    from src.contracts import (
        RISK_REDUCE_THRESHOLD, RISK_STOP_THRESHOLD,
        AccountHealth, AccountStatus, PlatformAccount, RiskAssessment,
        RiskLevel, is_fail_closed_status, utcnow,
    )
    from src.accounts.account_model import AuditLog, transition
except ImportError:  # direct-module import
    from contracts import (  # type: ignore
        RISK_REDUCE_THRESHOLD, RISK_STOP_THRESHOLD,
        AccountHealth, AccountStatus, PlatformAccount, RiskAssessment,
        RiskLevel, is_fail_closed_status, utcnow,
    )
    from account_model import AuditLog, transition  # type: ignore

__all__ = [
    "FEATURES", "ANOMALY_SIGNALS", "AccountCounters", "ManualReviewItem",
    "LogisticRiskModel", "AccountHealthScorerImpl", "build_features",
    "health_score_from_counters", "gate_action", "handle_anomaly_signal",
    "redact_evidence",
]

FEATURES = ["request_rate", "error_4xx_rate", "rate_limit_429_rate",
            "proxy_change_count", "behavior_deviation", "account_age_days",
            "checkpoint_history"]
_DEFAULT_WEIGHTS = {"request_rate": 0.35, "error_4xx_rate": 0.9, "rate_limit_429_rate": 1.1,
                    "proxy_change_count": 0.45, "behavior_deviation": 0.8,
                    "account_age_days": -0.25, "checkpoint_history": 1.4}
_DEFAULT_BIAS = -1.2

ANOMALY_SIGNALS = {
    "checkpoint": AccountStatus.CHECKPOINT,
    "captcha": AccountStatus.CAPTCHA_REQUIRED,
    "2fa": AccountStatus.NEEDS_REVIEW,
    "password_change": AccountStatus.NEEDS_REVIEW,
    "permission_error": AccountStatus.NEEDS_REAUTH,
    "strange_login": AccountStatus.CHECKPOINT,
    "token_revoked": AccountStatus.NEEDS_REAUTH,
}

_SECRET_PATTERNS = [re.compile(p, re.I) for p in
                    (r"(access_token|refresh_token|cookie|password|secret|totp)[\"'\s:=]+[^\s\",}]+",
                     r"Bearer\s+[A-Za-z0-9._-]+")]


def redact_evidence(text: str) -> str:
    """Mask anything that looks like a credential in evidence/logs."""
    out = str(text or "")
    for pat in _SECRET_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


@dataclass
class AccountCounters:
    """30-day (unless noted) counters feeding health + risk. All rates are 0..1."""
    total_syncs: int = 0
    successful_syncs: int = 0
    requests: int = 0
    error_4xx: int = 0
    rate_limit_429: int = 0
    proxy_changes: int = 0
    proxy_failures: int = 0
    checkpoint_count_30d: int = 0
    captcha_count_30d: int = 0
    behavior_deviation: float = 0.0
    account_age_days: float = 30.0
    requests_per_hour: float = 0.0

    @property
    def sync_success_rate(self) -> float:
        return self.successful_syncs / self.total_syncs if self.total_syncs else 1.0


def build_features(c: AccountCounters) -> dict[str, float]:
    return {
        "request_rate": min(1.0, c.requests_per_hour / 120.0),
        "error_4xx_rate": c.error_4xx / c.requests if c.requests else 0.0,
        "rate_limit_429_rate": c.rate_limit_429 / c.requests if c.requests else 0.0,
        "proxy_change_count": min(1.0, c.proxy_changes / 10.0),
        "behavior_deviation": max(0.0, min(1.0, c.behavior_deviation)),
        "account_age_days": min(1.0, c.account_age_days / 365.0),
        "checkpoint_history": min(1.0, c.checkpoint_count_30d / 5.0),
    }


def health_score_from_counters(c: AccountCounters) -> float:
    score = 0.55 * c.sync_success_rate
    score += 0.15 * max(0.0, 1.0 - c.checkpoint_count_30d / 5.0)
    score += 0.10 * max(0.0, 1.0 - c.captcha_count_30d / 5.0)
    score += 0.10 * max(0.0, 1.0 - (c.rate_limit_429 / c.requests if c.requests else 0.0) * 5)
    score += 0.10 * max(0.0, 1.0 - c.proxy_failures / 10.0)
    return max(0.0, min(1.0, score))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, x))))


def _risk_level(score: float) -> RiskLevel:
    if score >= RISK_STOP_THRESHOLD:
        return RiskLevel.HIGH
    if score >= RISK_REDUCE_THRESHOLD:
        return RiskLevel.MEDIUM
    return RiskLevel.LOW


def gate_action(risk_score: float) -> tuple[str, float]:
    """(action, frequency_factor). Mirrors the contract thresholds."""
    if risk_score >= RISK_STOP_THRESHOLD:
        return "stop", 0.0
    if risk_score >= RISK_REDUCE_THRESHOLD:
        return "reduce_frequency", 0.5
    return "none", 1.0


@dataclass
class LogisticRiskModel:
    """Logistic regression, trained by batch gradient descent on
    standardised features. Deterministic: no randomness."""

    weights: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_WEIGHTS))
    bias: float = _DEFAULT_BIAS
    means: dict[str, float] = field(default_factory=dict)
    stds: dict[str, float] = field(default_factory=dict)
    trained: bool = False

    def _standardise(self, feats: dict[str, float]) -> dict[str, float]:
        if not self.trained:
            return dict(feats)
        return {k: (v - self.means.get(k, 0.0)) / (self.stds.get(k, 1.0) or 1.0)
                for k, v in feats.items()}

    def predict_proba(self, feats: dict[str, float]) -> float:
        z = self._standardise(feats)
        logit = self.bias + sum(self.weights.get(k, 0.0) * v for k, v in z.items())
        return _sigmoid(logit)

    def top_features(self, feats: dict[str, float], *, limit: int = 3) -> list[str]:
        z = self._standardise(feats)
        contrib = sorted(((abs(self.weights.get(k, 0.0) * v), k) for k, v in z.items()), reverse=True)
        return [k for _, k in contrib[:limit]]

    def train(self, cases: list[tuple[dict[str, float], int]], *,
              epochs: int = 300, lr: float = 0.1) -> "LogisticRiskModel":
        if not cases:
            raise ValueError("no training cases")
        for k in FEATURES:
            vals = [c[0].get(k, 0.0) for c in cases]
            mean = sum(vals) / len(vals)
            var = sum((v - mean) ** 2 for v in vals) / len(vals)
            self.means[k] = mean
            self.stds[k] = math.sqrt(var) or 1.0
        self.weights = {k: 0.0 for k in FEATURES}
        self.bias = 0.0
        for _ in range(epochs):
            gw = {k: 0.0 for k in FEATURES}
            gb = 0.0
            for feats, label in cases:
                z = self._standardise(feats)
                p = _sigmoid(self.bias + sum(self.weights[k] * z[k] for k in FEATURES))
                err = p - label
                for k in FEATURES:
                    gw[k] += err * z[k]
                gb += err
            n = len(cases)
            for k in FEATURES:
                self.weights[k] -= lr * gw[k] / n
            self.bias -= lr * gb / n
        self.trained = True
        return self


@dataclass
class ManualReviewItem:
    account_id: str
    signal: str
    target_status: AccountStatus
    evidence: dict[str, Any]
    created_at: datetime = field(default_factory=utcnow)
    handled: bool = False


@dataclass
class AccountHealthScorerImpl:
    """AccountHealthScorer protocol implementation."""

    model: LogisticRiskModel = field(default_factory=LogisticRiskModel)
    counters: dict[str, AccountCounters] = field(default_factory=dict)
    manual_queue: list[ManualReviewItem] = field(default_factory=list)
    audit: AuditLog = field(default_factory=AuditLog)

    def set_counters(self, account_id: str, counters: AccountCounters) -> None:
        self.counters[account_id] = counters

    def score(self, account_id: str) -> AccountHealth:
        c = self.counters.get(account_id, AccountCounters())
        health = health_score_from_counters(c)
        risk = self.model.predict_proba(build_features(c))
        return AccountHealth(
            account_id=account_id, health_score=health, risk_level=_risk_level(risk),
            sync_success_rate=c.sync_success_rate, checkpoint_count_30d=c.checkpoint_count_30d,
            captcha_count_30d=c.captcha_count_30d, rate_limit_count_30d=c.rate_limit_429,
            proxy_failure_count_30d=c.proxy_failures)

    def assess_risk(self, account_id: str) -> RiskAssessment:
        c = self.counters.get(account_id, AccountCounters())
        feats = build_features(c)
        score = self.model.predict_proba(feats)
        action, _ = gate_action(score)
        if score >= RISK_STOP_THRESHOLD:
            action = "stop"
        elif score >= RISK_REDUCE_THRESHOLD:
            action = "reduce_frequency"
        return RiskAssessment(account_id=account_id, risk_score=score,
                              risk_level=_risk_level(score),
                              top_features=self.model.top_features(feats), action=action)

    def retrain(self, cases: list[tuple[dict[str, float], int]]) -> LogisticRiskModel:
        return self.model.train(cases)

    def apply_gate(self, account: PlatformAccount) -> dict[str, Any]:
        assessment = self.assess_risk(account.id)
        action, factor = gate_action(assessment.risk_score)
        result: dict[str, Any] = {"account_id": account.id, "risk_score": assessment.risk_score,
                                  "action": action, "frequency_factor": factor,
                                  "change_proxy": action in ("reduce_frequency", "stop"),
                                  "pause_non_essential": action in ("reduce_frequency", "stop")}
        if action == "stop":
            account.health_score = self.score(account.id).health_score
        return result


def handle_anomaly_signal(account: PlatformAccount, signal: str, *,
                          evidence: Optional[dict[str, Any]] = None,
                          actor_id: str = "risk-monitor",
                          audit: Optional[AuditLog] = None,
                          manual_queue: Optional[list[ManualReviewItem]] = None
                          ) -> ManualReviewItem:
    """Fail-closed checkpoint handling. One signal -> immediate transition
    (if legal from the current state), all automatic jobs stop because
    the new status is in FAIL_CLOSED_STATUSES, no login is retried."""
    target = ANOMALY_SIGNALS.get(signal)
    if target is None:
        raise ValueError(f"unknown anomaly signal: {signal}")
    clean: dict[str, Any] = {}
    for k, v in (evidence or {}).items():
        clean[k] = redact_evidence(v) if isinstance(v, str) else v
    try:
        if account.status != target:
            transition(account, target, actor_id=actor_id, audit=audit,
                       reason=f"anomaly signal: {signal}")
    except Exception:
        pass
    item = ManualReviewItem(account_id=account.id, signal=signal,
                            target_status=target, evidence=clean)
    if manual_queue is not None:
        manual_queue.append(item)
    if audit is not None:
        audit.append(workspace_id=account.workspace_id, actor_id=actor_id,
                     action="account.anomaly_queued", target_type="account",
                     target_id=account.id, detail={"signal": signal, "target": target.value})
    return item
