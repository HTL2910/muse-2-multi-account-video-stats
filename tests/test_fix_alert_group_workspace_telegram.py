"""Tests for fix_alert_group_workspace_telegram (backend-sec).

assign/remove must reject cross-workspace accounts fail-closed with
an audit trail and no partial mutation; the Telegram notifier must
send {chat_id, text} (Bot API sendMessage contract) via its own
chat_id config; channel send failures must land in metrics and the
observability sink, not only in an unread errors list.
Run from repo root: python -m pytest tests/test_fix_alert_group_workspace_telegram.py -q
"""
import dataclasses
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from src.api.alert_rule_engine import (
    GroupTagStore, MultiChannelNotifier, NotificationCenter,
    WebhookNotifier, WorkspaceMismatchError,
)
from src.contracts import (
    AccountStatus, Alert, AlertSeverity, AlertType, Platform,
    PlatformAccount,
)


def _account(aid, workspace_id):
    kwargs = dict(id=aid, platform=Platform.FACEBOOK, platform_user_id=aid,
                  status=AccountStatus.ACTIVE)
    fields = {f.name for f in dataclasses.fields(PlatformAccount)}
    if "workspace_id" in fields:
        kwargs["workspace_id"] = workspace_id
    acc = PlatformAccount(**kwargs)
    if "workspace_id" not in fields:  # very old contract stub
        acc.workspace_id = workspace_id
    return acc


def _alert():
    return Alert(id="al-1", workspace_id="ws-a",
                 alert_type=AlertType.ACCOUNT_BANNED,
                 severity=AlertSeverity.CRITICAL,
                 title="Account banned", message="acc-1 banned",
                 account_id="acc-1")


# ---------------------------------------------------------------- workspace enforcement
def test_assign_same_workspace_ok_and_audited():
    store = GroupTagStore()
    g = store.create_group(workspace_id="ws-a", name="A team")
    accs = [_account("a1", "ws-a"), _account("a2", "ws-a")]
    out = store.assign_bulk(group_id=g.id, account_ids=["a1", "a2"],
                            workspace_id="ws-a", accounts=accs)
    assert out.account_ids == ["a1", "a2"]
    assert store.audit_log[-1]["result"] == "ok"
    assert store.audit_log[-1]["action"] == "assign"


def test_assign_cross_workspace_account_rejected_atomically():
    store = GroupTagStore()
    g = store.create_group(workspace_id="ws-a", name="A team")
    accs = [_account("a1", "ws-a"), _account("b1", "ws-b")]  # b1 is tenant B
    with pytest.raises(WorkspaceMismatchError):
        store.assign_bulk(group_id=g.id, account_ids=["a1", "b1"],
                          workspace_id="ws-a", accounts=accs)
    assert store.get(g.id).account_ids == [], "no partial assignment allowed"
    rejected = [e for e in store.audit_log if e["result"] == "rejected"]
    assert len(rejected) == 1 and "b1" in rejected[0]["reason"]


def test_assign_caller_from_other_workspace_rejected():
    store = GroupTagStore()
    g = store.create_group(workspace_id="ws-a", name="A team")
    with pytest.raises(WorkspaceMismatchError):
        store.assign_bulk(group_id=g.id, account_ids=["a1"],
                          workspace_id="ws-b")  # caller is tenant B
    assert store.get(g.id).account_ids == []


def test_assign_unknown_account_in_registry_rejected():
    store = GroupTagStore()
    g = store.create_group(workspace_id="ws-a", name="A team")
    with pytest.raises(WorkspaceMismatchError):
        store.assign_bulk(group_id=g.id, account_ids=["ghost"],
                          workspace_id="ws-a",
                          accounts=[_account("a1", "ws-a")])
    assert store.get(g.id).account_ids == []


def test_assign_with_dict_registry():
    store = GroupTagStore()
    g = store.create_group(workspace_id="ws-a", name="A team")
    registry = {"a1": "ws-a", "b1": _account("b1", "ws-b")}
    store.assign_bulk(group_id=g.id, account_ids=["a1"],
                      workspace_id="ws-a", accounts=registry)
    assert store.get(g.id).account_ids == ["a1"]
    with pytest.raises(WorkspaceMismatchError):
        store.assign_bulk(group_id=g.id, account_ids=["b1"],
                          workspace_id="ws-a", accounts=registry)


def test_remove_cross_workspace_rejected_and_audited():
    store = GroupTagStore()
    g = store.create_group(workspace_id="ws-a", name="A team")
    store.assign_bulk(group_id=g.id, account_ids=["a1"],
                      workspace_id="ws-a",
                      accounts=[_account("a1", "ws-a")])
    with pytest.raises(WorkspaceMismatchError):
        store.remove_bulk(group_id=g.id, account_ids=["a1"],
                          workspace_id="ws-b")
    assert store.get(g.id).account_ids == ["a1"], "rejected remove must not mutate"
    assert any(e["action"] == "remove" and e["result"] == "rejected"
               for e in store.audit_log)
    # same-workspace remove still works
    store.remove_bulk(group_id=g.id, account_ids=["a1"], workspace_id="ws-a")
    assert store.get(g.id).account_ids == []


# ---------------------------------------------------------------- telegram payload
def test_telegram_payload_has_chat_id_and_text():
    sent = []
    notifier = WebhookNotifier(
        kind="telegram", url="https://api.telegram.org/botTOKEN/sendMessage",
        sender=lambda url, payload: sent.append((url, payload)),
        chat_id="-1001234567890")
    notifier.notify(_alert())
    assert len(sent) == 1
    url, payload = sent[0]
    assert payload["chat_id"] == "-1001234567890"
    assert "Account banned" in payload["text"]
    assert set(payload) == {"chat_id", "text"}


def test_telegram_chat_id_from_url_fallback():
    sent = []
    notifier = WebhookNotifier(
        kind="telegram",
        url="https://api.telegram.org/botTOKEN/sendMessage?chat_id=4242",
        sender=lambda url, payload: sent.append(payload))
    notifier.notify(_alert())
    assert sent[0]["chat_id"] == "4242"


def test_telegram_without_chat_id_fails_loudly():
    notifier = WebhookNotifier(
        kind="telegram", url="https://api.telegram.org/botTOKEN/sendMessage",
        sender=lambda url, payload: None)
    with pytest.raises(ValueError):
        notifier.notify(_alert())


def test_discord_and_generic_payloads_unchanged():
    sent = []
    WebhookNotifier(kind="discord", url="https://discord/hook",
                    sender=lambda u, p: sent.append(p)).notify(_alert())
    assert set(sent[0]) == {"content"}
    sent.clear()
    WebhookNotifier(kind="webhook", url="https://hook",
                    sender=lambda u, p: sent.append(p)).notify(_alert())
    assert sent[0]["alert_type"] == "account_banned"
    assert sent[0]["account_id"] == "acc-1"


# ---------------------------------------------------------------- channel failure metrics
class _Boom:
    kind = "telegram"

    def notify(self, alert):
        raise RuntimeError("telegram 400: chat not found")


def test_channel_failure_counted_in_metrics_and_sink():
    center = NotificationCenter()
    sink_calls = []
    multi = MultiChannelNotifier(
        channels=[_Boom(), center],
        metrics_sink=lambda name, exc, alert: sink_calls.append((name, exc)))
    multi.notify(_alert())
    assert len(center.alerts) == 1, "one bad channel must not block others"
    assert multi.metrics["failed"] == 1 and multi.metrics["sent"] == 1
    assert multi.metrics["by_channel"]["telegram"]["failed"] == 1
    assert multi.metrics["by_channel"]["NotificationCenter"]["sent"] == 1
    assert multi.failure_count == 1
    assert len(multi.errors) == 1  # legacy list still kept
    assert len(sink_calls) == 1 and sink_calls[0][0] == "telegram"
    assert isinstance(sink_calls[0][1], RuntimeError)


def test_telegram_400_scenario_flows_into_metrics():
    # the original production failure: telegram without chat_id -> 400s
    bad = WebhookNotifier(kind="telegram",
                          url="https://api.telegram.org/botT/sendMessage",
                          sender=lambda u, p: None)  # no chat_id anywhere
    center = NotificationCenter()
    multi = MultiChannelNotifier(channels=[bad, center])
    multi.notify(_alert())
    assert multi.metrics["by_channel"]["telegram"]["failed"] == 1
    assert len(center.alerts) == 1
