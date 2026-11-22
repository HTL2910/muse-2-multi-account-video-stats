"""fix_import_dual_contracts — mot kieu import contracts duy nhat.

Quy tac da khoa: chi ``from src.contracts import ...`` khi chay tu repo
root. Khong fallback ``from contracts import`` / ``from ..contracts
import`` (va khong fallback sai ``from account_model import ...`` o
risk — phan do thuoc file cua no, khong sua o day).

Test nay tu dua repo root len sys.path (khong phu thuoc conftest.py hay
pyproject.toml, vi 2 file do khong thuoc pham vi nhanh fix/* nay) va
kiem tra ca o muc source lan o muc runtime: chi ton tai mot module
object ``src.contracts``, cac Enum/dataclass account_model dung la
cung object do, va hanh vi state machine khong doi.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.accounts.account_model as account_model
import src.contracts as contracts
from src.contracts import AccountStatus, AuthMethod, Platform

SOURCE = (REPO_ROOT / "src" / "accounts" / "account_model.py").read_text(encoding="utf-8")

# Moi dong import contracts khong phai dang chuan, ke ca fallback trong
# try/except va fallback sai kieu 'from account_model import ...'.
FORBIDDEN_IMPORTS = [
    re.compile(r"^\s*from contracts import\b", re.M),
    re.compile(r"^\s*from \.\.?contracts import\b", re.M),
    re.compile(r"^\s*from account_model import\b", re.M),
    re.compile(r"^\s*import contracts\b", re.M),
]


def test_source_has_only_canonical_contracts_import():
    assert "from src.contracts import" in SOURCE
    for pat in FORBIDDEN_IMPORTS:
        assert not pat.search(SOURCE), f"source chua import fallback: {pat.pattern!r}"
    assert "except ImportError" not in SOURCE, (
        "khong duoc giu try/except ImportError fallback cho contracts"
    )


def test_single_contracts_module_object():
    # account_model dung chinh object src.contracts, khong phai ban sao.
    assert account_model.Platform is contracts.Platform
    assert account_model.AccountStatus is contracts.AccountStatus
    assert account_model.PlatformAccount is contracts.PlatformAccount
    assert account_model.TokenSet is contracts.TokenSet
    assert account_model.AuditLogEntry is contracts.AuditLogEntry
    assert account_model.utcnow is contracts.utcnow
    assert account_model.ALLOWED_ACCOUNT_TRANSITIONS is contracts.ALLOWED_ACCOUNT_TRANSITIONS
    # Trong process nay khong ton tai module top-level 'contracts' rieng.
    assert "contracts" not in sys.modules
    assert sys.modules["src.contracts"] is contracts


def test_import_in_fresh_process_creates_no_bare_contracts_module():
    code = (
        "import sys; sys.path.insert(0, '.'); "
        "import src.accounts.account_model; "
        "print('contracts' in sys.modules); "
        "print(sys.modules['src.contracts'].__name__)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    lines = out.stdout.strip().splitlines()
    assert lines == ["False", "src.contracts"]


def test_state_machine_behaviour_unchanged():
    a = account_model.create_account(
        workspace_id="ws-1", platform=Platform.FACEBOOK, platform_user_id="u123"
    )
    assert a.status is AccountStatus.CREATED
    assert isinstance(a.status, contracts.AccountStatus)
    assert account_model.natural_key(Platform.FACEBOOK, " u123 ") == "facebook:u123"
    assert account_model.validate_account(a) == []

    audit = account_model.AuditLog()
    account_model.transition(a, AccountStatus.VERIFIED, actor_id="t", audit=audit)
    assert a.status is AccountStatus.VERIFIED
    assert audit.verify() == []

    with pytest.raises(account_model.InvalidTransitionError):
        # CREATED -> ACTIVE la illegal (chi qua VERIFIED); khong duoc ep kieu.
        account_model.transition(
            account_model.create_account(
                workspace_id="ws-1", platform=Platform.TIKTOK, platform_user_id="u9"
            ),
            AccountStatus.ACTIVE,
            actor_id="t",
        )


def test_repository_upsert_still_idempotent_on_natural_key():
    repo = account_model.AccountRepository()
    a1 = account_model.create_account(
        workspace_id="ws-1", platform=Platform.YOUTUBE, platform_user_id="ch1",
        display_name="A",
    )
    a2 = account_model.create_account(
        workspace_id="ws-1", platform=Platform.YOUTUBE, platform_user_id="ch1",
        display_name="B",
    )
    _, created1 = repo.upsert(a1)
    stored, created2 = repo.upsert(a2)
    assert created1 is True and created2 is False
    assert stored.id == a1.id and stored.display_name == "B"
    assert repo.get_by_natural(Platform.YOUTUBE, "ch1") is stored
