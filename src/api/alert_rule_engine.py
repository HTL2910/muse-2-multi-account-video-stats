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

Fix ``fix_alert_group_workspace_telegram`` (gop
fix_alert_group_workspace_and_telegram_p):
- assign_bulk/remove_bulk: truoc day chi nhan group_id + account_ids,
  khong doi chieu workspace nen workspace A gan duoc account cua
  workspace B vao group cua minh (lo hong cach ly tenant). Nay ca
  hai ham nhan them ``workspace_id`` (workspace cua caller) va
  ``accounts`` (registry de tra workspace cua tung account); moi
  account khong cung workspace voi group — hoac khong tra duoc trong
  registry da cung cap — deu bi TU CHOI fail-closed bang
  WorkspaceMismatchError, kiem tra truoc khi mutate nen khong co gan
  mot phan, va moi lan gan/go (thanh cong hay bi tu choi) deu ghi
  vao audit_log append-only cua store.
- WebhookNotifier kind="telegram": truoc day gui payload chi co
  {"text": ...} trong khi Telegram Bot API sendMessage BAT BUOC
  chat_id + text -> luon bi 400. Nay notifier nhan ``chat_id``
  rieng trong cau hinh (du phong: tham so chat_id tren URL) va gui
  {"chat_id": ..., "text": ...}; thieu chat_id thi raise ngay thay
  vi gui payload chac chan loi.
- MultiChannelNotifier: truoc day loi gui kenh chi bi nuot vao list
  ``errors`` khong ai doc. Nay moi loi deu duoc dem vao ``metrics``
  (sent/failed tong va theo tung kenh), ghi log canh bao co cau
  truc, va goi ``metrics_sink`` tiem vao neu co (noi observability
  dem canh bao that bai) — list errors van giu de tuong thich.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
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
           "NotificationCenter", "WebhookNotifier", "MultiChannelNotifier",
           "WorkspaceMismatchError"]

Sender = Callable[[str, dict], None]  # (url, payload) -> None

_log = logging.getLogger("api.alert_rule_engine")


class WorkspaceMismatchError(PermissionError):
    """Gan/go account khac workspace vao group - fail-closed."""


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
        # Append-only audit cho assign/remove: ca thanh cong lan bi
        # tu choi deu ghi lai (ai gan account nao vao group nao, o
        # workspace nao, ket qua gi).
        self.audit_log: list[dict] = []

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

    # -- kiem tra workspace (fail-closed) ------------------------------
    @staticmethod
    def _workspace_index(accounts) -> Optional[dict[str, str]]:
        """Chuan hoa ``accounts`` (list PlatformAccount hoac dict
        id -> PlatformAccount/workspace_id) thanh id -> workspace_id.
        None neu caller khong cung cap registry."""
        if accounts is None:
            return None
        index: dict[str, str] = {}
        if isinstance(accounts, dict):
            items = accounts.items()
            for aid, value in items:
                index[aid] = value if isinstance(value, str) \
                    else str(getattr(value, "workspace_id", ""))
            return index
        for a in accounts:
            index[a.id] = a.workspace_id
        return index

    def _check_workspace(self, action: str, g: AccountGroup,
                         account_ids: list[str],
                         workspace_id: Optional[str], accounts) -> None:
        """Nem WorkspaceMismatchError (+ audit) neu bat ky kiem tra
        workspace nao fail. Chay TRUOC khi mutate nen thao tac hoac
        thanh cong toan bo, hoac khong doi gi ca."""
        reason = ""
        if workspace_id is not None and workspace_id != g.workspace_id:
            reason = (f"caller workspace {workspace_id} != group "
                      f"workspace {g.workspace_id}")
        index = self._workspace_index(accounts)
        if not reason and index is not None:
            for aid in account_ids:
                ws = index.get(aid)
                if ws is None:
                    reason = f"account {aid} not in provided registry"
                    break
                if ws != g.workspace_id:
                    reason = (f"account {aid} belongs to workspace {ws}, "
                              f"group is in {g.workspace_id}")
                    break
        if reason:
            self._audit(action, g, workspace_id, account_ids,
                        result="rejected", reason=reason)
            _log.warning(json.dumps(
                {"level": "warning", "component": "alerts",
                 "event": "group_workspace_mismatch", "action": action,
                 "group_id": g.id, "reason": reason}, sort_keys=True))
            raise WorkspaceMismatchError(reason)

    def _audit(self, action: str, g: AccountGroup,
               workspace_id: Optional[str], account_ids: list[str],
               *, result: str, reason: str = "") -> None:
        self.audit_log.append({
            "action": action, "group_id": g.id,
            "group_workspace_id": g.workspace_id,
            "caller_workspace_id": workspace_id,
            "account_ids": list(account_ids), "result": result,
            "reason": reason, "at": utcnow().isoformat()})

    def assign_bulk(self, *, group_id: str, account_ids: list[str],
                    workspace_id: Optional[str] = None,
                    accounts=None) -> AccountGroup:
        """Gan account vao group, CHI khi moi account cung workspace
        voi group. ``workspace_id`` la workspace cua caller;
        ``accounts`` la registry (list PlatformAccount hoac dict) de
        doi chieu workspace cua tung account. Lech workspace ->
        WorkspaceMismatchError, khong gan mot phan."""
        with self._lock:
            g = self._groups[group_id]
            self._check_workspace("assign", g, account_ids,
                                  workspace_id, accounts)
            for aid in account_ids:
                if aid not in g.account_ids:
                    g.account_ids.append(aid)
            self._audit("assign", g, workspace_id, account_ids, result="ok")
            return g

    def remove_bulk(self, *, group_id: str, account_ids: list[str],
                    workspace_id: Optional[str] = None,
                    accounts=None) -> AccountGroup:
        """Go account khoi group voi cung kiem tra workspace nhu
        assign_bulk: caller/account khac workspace voi group thi bi
        tu choi fail-closed + audit, khong go mot phan."""
        with self._lock:
            g = self._groups[group_id]
            self._check_workspace("remove", g, account_ids,
                                  workspace_id, accounts)
            drop = set(account_ids)
            g.account_ids = [a for a in g.account_ids if a not in drop]
            self._audit("remove", g, workspace_id, account_ids, result="ok")
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
    """Telegram / Discord / generic webhook.

    Telegram: ``chat_id`` la cau hinh rieng bat buoc (Bot API
    sendMessage can chat_id + text). Neu chua dat, thu doc tham so
    chat_id tren URL (tuong thich cau hinh cu); van khong co thi
    notify() raise ValueError thay vi gui payload chac chan bi 400.
    """
    kind: str  # telegram | discord | webhook
    url: str
    sender: Sender = _http_sender
    chat_id: str = ""

    def _telegram_chat_id(self) -> str:
        if self.chat_id:
            return str(self.chat_id)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.url).query)
        values = query.get("chat_id") or []
        return str(values[0]) if values else ""

    def notify(self, alert: Alert) -> None:
        text = f"[{alert.severity.value.upper()}] {alert.title}: {alert.message}"
        if self.kind == "telegram":
            chat_id = self._telegram_chat_id()
            if not chat_id:
                raise ValueError(
                    "telegram notifier requires chat_id (config field "
                    "or ?chat_id= on the URL) - refusing to send a "
                    "payload the Bot API would reject with 400")
            payload = {"chat_id": chat_id, "text": text}
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
    """Gui alert qua nhieu kenh; 1 kenh loi khong chan kenh khac.

    Loi gui kenh KHONG con chi nam trong list ``errors``: moi ket
    qua deu duoc dem vao ``metrics`` ({"sent", "failed",
    "by_channel": {ten_kenh: {"sent", "failed"}}}), loi ghi log
    canh bao co cau truc va goi ``metrics_sink`` (channel_name,
    exception, alert) neu duoc tiem — noi observability dem so lan
    gui canh bao that bai. ``errors`` van giu de tuong thich.
    """
    channels: list = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    metrics_sink: Optional[Callable[[str, Exception, Alert], None]] = None
    metrics: dict = field(default_factory=lambda: {
        "sent": 0, "failed": 0, "by_channel": {}})
    _lock: threading.Lock = field(default_factory=threading.Lock,
                                  repr=False, compare=False)

    @property
    def failure_count(self) -> int:
        return self.metrics["failed"]

    @staticmethod
    def _channel_name(ch) -> str:
        return str(getattr(ch, "kind", "") or type(ch).__name__)

    def _record(self, name: str, ok: bool) -> None:
        with self._lock:
            bucket = self.metrics["by_channel"].setdefault(
                name, {"sent": 0, "failed": 0})
            if ok:
                self.metrics["sent"] += 1
                bucket["sent"] += 1
            else:
                self.metrics["failed"] += 1
                bucket["failed"] += 1

    def notify(self, alert: Alert) -> None:
        for ch in self.channels:
            name = self._channel_name(ch)
            try:
                ch.notify(alert)
            except Exception as exc:  # 1 kenh loi khong chan kenh khac
                self.errors.append(f"{type(ch).__name__}: {exc}")
                self._record(name, ok=False)
                _log.warning(json.dumps(
                    {"level": "warning", "component": "alerts",
                     "event": "alert_channel_failed", "channel": name,
                     "alert_id": alert.id, "error": str(exc)},
                    sort_keys=True))
                if self.metrics_sink is not None:
                    try:
                        self.metrics_sink(name, exc, alert)
                    except Exception:
                        pass  # sink loi khong duoc lam hong notify
            else:
                self._record(name, ok=True)


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
