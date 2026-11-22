"""fix_import_dual_contracts_residual — chi con mot cach import contracts.

Vong 1 (fix_import_dual_contracts) moi sua src/accounts/account_model.py.
File nay, src/core/models.py, van giu try/except hai kieu — nguy hiem
nhat vi PlatformAdapterBase.guard() duoc moi connector import: fallback
``..contracts`` co the nap cung mot file thanh module thu hai
(``contracts`` tran vs ``src.contracts``) lam Enum/dataclass identity
(``is`` / ``isinstance``) am tham vo giua cac module, ma pytest khong
bat duoc vi no luon import qua ``src.*`` tu repo root.

Test tinh (AST + text) + smoke test import identity. Test tu dua repo
root len sys.path de khong phu thuoc conftest/pyproject.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

CORE_PATH = REPO_ROOT / "src" / "core" / "models.py"
ACCOUNT_MODEL_PATH = REPO_ROOT / "src" / "accounts" / "account_model.py"

# Mau fallback bi cam (grep/AST): import tran hoac import tuong doi.
BANNED_IMPORT_RE = re.compile(
    r"^\s*(from\s+contracts\s+import|from\s+\.+contracts\s+import"
    r"|from\s+account_model\s+import|from\s+\.+account_model\s+import)\b",
    re.M,
)


def _contracts_imports(path: Path) -> list[ast.ImportFrom]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [n for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module
            and n.module.endswith("contracts")]


def _has_importerror_fallback_import(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        for handler in node.handlers:
            if handler.type is None:
                continue
            handler_name = ast.unparse(handler.type)
            if "ImportError" not in handler_name:
                continue
            for sub in ast.walk(handler):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    return True
    return False


def test_core_models_has_exactly_one_src_contracts_import():
    imports = _contracts_imports(CORE_PATH)
    assert len(imports) == 1, [ast.unparse(i) for i in imports]
    only = imports[0]
    assert only.module == "src.contracts" and only.level == 0
    assert not _has_importerror_fallback_import(CORE_PATH)


def test_core_models_text_has_no_fallback_pattern_or_dual_claim():
    text = CORE_PATH.read_text(encoding="utf-8")
    assert not BANNED_IMPORT_RE.search(text)
    assert "except ImportError" not in text
    assert "ca 2 cach" not in text
    assert "..contracts" not in text
    assert "from src.contracts import" in text


def test_account_model_still_single_import():
    # Bao ve fix vong 1 khoi hoi quy.
    imports = _contracts_imports(ACCOUNT_MODEL_PATH)
    assert len(imports) == 1
    assert imports[0].module == "src.contracts" and imports[0].level == 0
    assert not _has_importerror_fallback_import(ACCOUNT_MODEL_PATH)
    assert not BANNED_IMPORT_RE.search(ACCOUNT_MODEL_PATH.read_text(encoding="utf-8"))


def test_no_bare_contracts_module_and_identity_is_single():
    import src.contracts as contracts
    import src.accounts.account_model as account_model
    import src.core.models as core

    assert "contracts" not in sys.modules, (
        "module 'contracts' tran da duoc nap — dual-module identity")
    for name in ("AccountStatus", "Platform", "PlatformAccount",
                 "UnifiedVideoMetric", "Video", "JobStatus", "TokenSet"):
        assert getattr(core, name) is getattr(contracts, name), name
        assert getattr(account_model, name, getattr(contracts, name)) is getattr(contracts, name), name
    # isinstance xuyen module phai dung: account tao tu contracts duoc
    # core chap nhan (guard/transition dung cung Enum object).
    # CREATED -> VERIFIED la chuyen doi hop le theo ALLOWED_ACCOUNT_TRANSITIONS.
    account = contracts.PlatformAccount(
        id="acc-1", workspace_id="ws-1", platform=contracts.Platform.FACEBOOK,
        platform_user_id="page-1", status=contracts.AccountStatus.CREATED,
        credential_ref="vault://facebook/acc-1",
    )
    assert isinstance(account, core.PlatformAccount)
    core.transition_account(account, contracts.AccountStatus.VERIFIED)
    assert account.status is contracts.AccountStatus.VERIFIED


def test_core_still_works_after_import_cleanup():
    import src.core.models as core
    import src.contracts as contracts

    service = core.CoreService()
    account = service.create_account(
        workspace_id="ws-1", platform=contracts.Platform.FACEBOOK,
        platform_user_id="page-1", credential_ref="vault://facebook/acc-1",
    )
    assert account.status is contracts.AccountStatus.CREATED
    active = contracts.PlatformAccount(
        id="acc-1", workspace_id="ws-1", platform=contracts.Platform.FACEBOOK,
        platform_user_id="page-1", status=contracts.AccountStatus.ACTIVE,
        credential_ref="vault://facebook/acc-1",
    )
    ok, reason = service.can_collect(active, None)
    assert not ok and "no proxy bound" in reason

    class _Adapter(core.PlatformAdapterBase):
        platform = contracts.Platform.FACEBOOK

        def authenticate(self, a): raise NotImplementedError
        def check_alive(self, a): return True
        def list_videos(self, a, *, limit=50): return []
        def get_video_stats(self, a, v): raise NotImplementedError
        def refresh_token(self, a, t): raise NotImplementedError

    adapter = _Adapter()
    adapter.guard(account)  # ACTIVE + credential_ref -> khong raise
    banned = contracts.PlatformAccount(
        id="acc-2", workspace_id="ws-1", platform=contracts.Platform.FACEBOOK,
        platform_user_id="page-2", status=contracts.AccountStatus.BANNED,
        credential_ref="vault://facebook/acc-2",
    )
    with pytest.raises(core.AdapterAuthError):
        adapter.guard(banned)

    metric = contracts.UnifiedVideoMetric(
        video_id="vid-1", account_id=account.id, platform=contracts.Platform.FACEBOOK,
        platform_video_id="v1", views=100, likes=5, comments=2,
        date_bucket="2026-11-22",
    )
    store = core.InMemoryMetricsStore()
    saved = store.save_snapshot(metric)
    assert store.get_latest(video_id="vid-1") is metric
    assert saved is metric  # hanh vi tren main: tra metric tran (fix rieng chua merge)
    assert store.daily_rollup(video_id="vid-1", date_bucket="2026-11-22").views == 100


def test_residual_inventory_core_and_account_model_not_in_it():
    """Chi khang dinh pham vi nhanh nay: core + account_model sach.
    Cac file khac van con fallback (ai, analytics, browser, monitor,
    risk, facebook/tiktok connector, scheduler) thuoc PR co hoc toan
    src/ tiep theo — test nay khong khang dinh gi ve chung de khong
    fail oan khi mot file trong so do duoc sua truoc."""
    residual = []
    for path in (REPO_ROOT / "src").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if BANNED_IMPORT_RE.search(text) or "except ImportError" in text:
            residual.append(str(path.relative_to(REPO_ROOT)))
    assert "src/core/models.py" not in residual, residual
    assert "src/accounts/account_model.py" not in residual, residual
