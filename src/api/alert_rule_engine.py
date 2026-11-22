"""approach_15 - Phan nhom, tagging va rule engine canh bao.

Tuan thu contracts: AccountGroup, Alert, AlertType, AlertSeverity,
PlatformAccount, AccountStatus, UnifiedVideoMetric, AlertNotifier.

- Group/Tag store in-memory sau interface ro rang (prod thay bang DB,
  van giu nguyen API nay). Gan hang loat co kiem tra workspace.
- Rule engine: danh gia su kien/thong ke -> Alert, co nguong cau hinh
  duoc, dedupe + cooldown de khong spam, consecutive-failure counter
  cho sync.
- Notifier: notification center (luu trong store) + Telegram/Webhook/
  Discord qua sender tiem vao (mac dinh urllib, test tiem fake).
  Loi gui 1 kenh khong lam chet cac kenh khac.
"""
from __future__ import annotations

import json
import threading
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Callable, Optional, Protocol

try:
    from src.contracts import (
        AccountGroup, AccountStatus, Alert, AlertSeverity, AlertType,
        PlatformAccount, UnifiedVideoMetric, Video, VideoStatus, utcnow,
    )
except ImportError:  # pragma: no cover
    from contracts import (  # type: ignore
        AccountGroup, AccountStatus, Alert, AlertSeverity, AlertType,
        PlatformAccount, UnifiedVideoMetric, Video, VideoStatus, utcnow,
    )

__all__ = ["GroupTagStore", "AlertRuleConfig", "AlertRuleEngine",
           "NotificationCenter", "WebhookNotifier", "MultiChannelNotifier"]

Sender = Callable[[str, dict], None]  # (url, payload) -> None


def _http_sender(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        resp.read()


# ---------------------------------------------------------------- Group/Tag
class GroupTagStore:
    def __init__(self) -> None:
        self._groups: dict[str, AccountGroup] = {}
        self._lock = threading.Lock()

    def create_group(self, *, workspace_id: str, name: str,
                     tags: list[str] | None = None) -> AccountGroup:
        g = AccountGroup(id=str(uuid.uuid4()), workspace_id=workspace_id,
                         name=name.strip(), tags=list(tags or []))
        if not g.name:
            raise ValueError("group name required")
        with self._lock:
            self._groups[g.id] = g
        return g

    def get(self, group_id: str) -> Optional[AccountGroup]:
        return self._groups.get(group_id)

    def assign_bulk(self, *, group_id: str, account_ids: list[str]) -> AccountGroup:
        with self._lock:
            g = self._groups[group_id]
            for aid in account_ids:
                if aid not in g.account_ids:
                    g.account_ids.append(aid)
            return g

    def remove_bulk(self, *, group_id: str, account_ids: list[str]) -> AccountGroup:
        with self._lock:
            g = self._groups[group_id]
            drop = set(account_ids)
            g.account_ids = [a for a in g.account_ids if a not in drop]
            return g

    def groups_of(self, account_id: str) -> list[AccountGroup]:
        return [g for g in self._groups.values() if account_id in g.account_ids]

    @staticmethod
    def add_tags(account: PlatformAccount, tags: list[str]) -> PlatformAccount:
        for t in tags:
            t = t.strip().lower()
            if t and t not in account.tags:
                account.tags.append(t)
        return account

    @staticmethod
    def remove_tags(account: PlatformAccount, tags: list[str]) -> PlatformAccount:
        drop = {t.strip().lower() for t in tags}
        account.tags = [t for t in account.tags if t not in drop]
        return account

    @staticmethod
    def filter_accounts(accounts: list[PlatformAccount], *,
                        group: AccountGroup | None = None,
                        tags_all: list[str] | None = None,
                        workspace_id: str | None = None) -> list[PlatformAccount]:
        out = []
        for a in accounts:
            if workspace_id and a.workspace_id != workspace_id:
                continue
            if group and a.id not in group.account_ids:
                continue
            if tags_all and not set(tags_all).issubset(set(a.tags)):
                continue
            out.append(a)
        return out


# ---------------------------------------------------------------- Notifiers
class NotificationCenter:
    """Implements contracts.AlertNotifier - luu alert de hien thi UI."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def notify(self, alert: Alert) -> None:
        self.alerts.append(alert)

    def unread(self, workspace_id: str) -> list[Alert]:
        return [a for a in self.alerts
                if a.workspace_id == workspace_id and not a.is_read]


@dataclass
class WebhookNotifier:
    """Telegram (chat_id qua URL bot) / Discord / generic webhook."""
    kind: str  # telegram | discord | webhook
    url: str
    sender: Sender = _http_sender

    def notify(self, alert: Alert) -> None:
        text = f"[{alert.severity.value.upper()}] {alert.title}: {alert.message}"
        if self.kind == "telegram":
            payload = {"text": text}
        elif self.kind == "discord":
            payload = {"content": text}
        else:
            payload = {"alert_type": alert.alert_type.value,
                       "severity": alert.severity.value, "title": alert.title,
                       "message": alert.message, "account_id": alert.account_id,
                       "video_id": alert.video_id}
        self.sender(self.url, payload)


@dataclass
class MultiChannelNotifier:
    channels: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def notify(self, alert: Alert) -> None:
        for ch in self.channels:
            try:
                ch.notify(alert)
            except Exception as exc:  # 1 kenh loi khong chan kenh khac
                self.errors.append(f"{type(ch).__name__}: {exc}")


# ---------------------------------------------------------------- Rule engine
@dataclass
class AlertRuleConfig:
    token_expiry_hours: int = 24
    spike_ratio: float = 3.0        # views gap >= 3x ky truoc
    spike_min_views: int = 1000
    drop_ratio: float = 0.5         # tut >= 50% (views am/snapshot giam)
    sync_fail_consecutive: int = 3
    cooldown_minutes: int = 60


class AlertRuleEngine:
    def __init__(self, notifier, *, config: AlertRuleConfig | None = None):
        self._notifier = notifier
        self.cfg = config or AlertRuleConfig()
        self._sync_fails: dict[str, int] = {}
        self._last_sent: dict[str, object] = {}
        self._lock = threading.Lock()

    def _emit(self, *, workspace_id: str, alert_type: AlertType,
              severity: AlertSeverity, title: str, message: str,
              account_id: str | None = None,
              video_id: str | None = None) -> Optional[Alert]:
        key = f"{alert_type.value}:{account_id or ''}:{video_id or ''}"
        now = utcnow()
        with self._lock:
            last = self._last_sent.get(key)
            if last and now - last < timedelta(minutes=self.cfg.cooldown_minutes):  # type: ignore
                return None  # dedupe/cooldown - chong spam canh bao
            self._last_sent[key] = now
        alert = Alert(id=str(uuid.uuid4()), workspace_id=workspace_id,
                      alert_type=alert_type, severity=severity,
                      account_id=account_id, video_id=video_id,
                      title=title, message=message)
        self._notifier.notify(alert)
        return alert

    # -- Account / proxy / token --------------------------------------
    def on_account_status(self, account: PlatformAccount) -> list[Alert]:
        out: list[Alert] = []
        mapping = {
            AccountStatus.CHECKPOINT: (AlertType.ACCOUNT_CHECKPOINT, AlertSeverity.CRITICAL),
            AccountStatus.CAPTCHA_REQUIRED: (AlertType.ACCOUNT_CHECKPOINT, AlertSeverity.CRITICAL),
            AccountStatus.BANNED: (AlertType.ACCOUNT_BANNED, AlertSeverity.CRITICAL),
            AccountStatus.SUSPENDED: (AlertType.ACCOUNT_BANNED, AlertSeverity.CRITICAL),
        }
        if account.status in mapping:
            at, sev = mapping[account.status]
            a = self._emit(workspace_id=account.workspace_id, alert_type=at,
                           severity=sev, title=f"Account {account.status.value}",
                           message=f"{account.display_name or account.id} on {account.platform.value} is {account.status.value}",
                           account_id=account.id)
            if a:
                out.append(a)
        return out

    def on_proxy_dead(self, *, workspace_id: str, proxy_id: str,
                      account_id: str | None = None) -> Optional[Alert]:
        return self._emit(workspace_id=workspace_id, alert_type=AlertType.PROXY_DEAD,
                          severity=AlertSeverity.CRITICAL, title="Proxy dead",
                          message=f"Proxy {proxy_id} is dead/quarantined",
                          account_id=account_id)

    def on_token_expiring(self, account: PlatformAccount, *,
                          expires_at) -> Optional[Alert]:
        if expires_at is None or expires_at > utcnow() + timedelta(hours=self.cfg.token_expiry_hours):
            return None
        return self._emit(workspace_id=account.workspace_id,
                          alert_type=AlertType.TOKEN_EXPIRING,
                          severity=AlertSeverity.WARNING, title="Token expiring",
                          message=f"Token expires at {expires_at.isoformat()} (<{self.cfg.token_expiry_hours}h)",
                          account_id=account.id)

    def on_quota(self, *, workspace_id: str, used_ratio: float) -> Optional[Alert]:
        if used_ratio < 0.8:
            return None
        return self._emit(workspace_id=workspace_id, alert_type=AlertType.QUOTA_WARNING,
                          severity=AlertSeverity.WARNING if used_ratio < 0.95 else AlertSeverity.CRITICAL,
                          title="API quota warning",
                          message=f"Quota used {used_ratio:.0%}")

    # -- Sync ------------------------------------------------------------
    def on_sync_result(self, account: PlatformAccount, *, ok: bool) -> Optional[Alert]:
        with self._lock:
            if ok:
                self._sync_fails[account.id] = 0
                return None
            n = self._sync_fails.get(account.id, 0) + 1
            self._sync_fails[account.id] = n
        if n < self.cfg.sync_fail_consecutive:
            return None
        return self._emit(workspace_id=account.workspace_id,
                          alert_type=AlertType.SYNC_FAILED,
                          severity=AlertSeverity.CRITICAL, title="Sync failed repeatedly",
                          message=f"{n} consecutive sync failures", account_id=account.id)

    # -- Video -------------------------------------------------------------
    def on_metric(self, *, workspace_id: str, account_id: str,
                  current: UnifiedVideoMetric,
                  previous: UnifiedVideoMetric | None) -> list[Alert]:
        out: list[Alert] = []
        if previous is None:
            return out
        if previous.views > 0 and current.views >= self.cfg.spike_min_views \
                and current.views >= previous.views * self.cfg.spike_ratio:
            a = self._emit(workspace_id=workspace_id, alert_type=AlertType.VIDEO_SPIKE,
                           severity=AlertSeverity.INFO, title="Video views spike",
                           message=f"Views {previous.views} -> {current.views}",
                           account_id=account_id, video_id=current.video_id)
            if a:
                out.append(a)
        if previous.views > 0 and current.views < previous.views * self.cfg.drop_ratio:
            a = self._emit(workspace_id=workspace_id, alert_type=AlertType.VIDEO_DROP,
                           severity=AlertSeverity.WARNING, title="Video views drop anomaly",
                           message=f"Views snapshot {previous.views} -> {current.views} (possible data/platform issue)",
                           account_id=account_id, video_id=current.video_id)
            if a:
                out.append(a)
        return out

    def on_video_list_diff(self, *, workspace_id: str, account_id: str,
                           previous: list[Video], current: list[Video]) -> list[Alert]:
        cur_ids = {v.platform_video_id for v in current}
        out: list[Alert] = []
        for v in previous:
            if v.platform_video_id not in cur_ids and v.status == VideoStatus.ACTIVE:
                a = self._emit(workspace_id=workspace_id, alert_type=AlertType.VIDEO_REMOVED,
                               severity=AlertSeverity.WARNING, title="Video removed/hidden",
                               message=f"Video {v.platform_video_id} missing from latest list",
                               account_id=account_id, video_id=v.id)
                if a:
                    out.append(a)
        return out
