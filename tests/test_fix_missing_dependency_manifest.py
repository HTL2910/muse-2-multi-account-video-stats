"""fix_missing_dependency_manifest — manifest phu thuoc tai lap duoc.

Kiem tra pyproject.toml la manifest duy nhat, pin day du, Python >= 3.11,
cryptography la dependency production (khong phai dev-only / optional) de
vault khong bao gio roi lang le ve fallback stdlib, va cac dependency da
cai dung khop pin (ke ca AES-256-GCM that).

Cong doi chieu merge (meta-gate): TRIAGED_FIXES_REQUIRED liet ke moi fix
da triage co file test tren main; neu mot merge lam mat file test cua
mot fix trong danh sach, test nay do — merge do coi nhu chua xong.
TRIAGED_FIXES_KNOWN_MISSING liet ke cac fix da nop nhung chua co file
test tren main (dang xfail, khong do suite); khi mot fix trong do merge
xong, chuyen no sang REQUIRED. Them fix moi da triage = them mot dong
vao REQUIRED cung voi file test cua no.
Luu y ve moi truong chay: gate chi co y nghia tren checkout main day du
(nhan dien bang file canary tests/test_systems_arch_m2.py). Khi CI chay
le file test cua mot nhanh (sandbox chi co pyproject + file test nay),
gate SKIP thay vi fail — vi thieu file o do khong phai do merge lam mat.
"""
from __future__ import annotations

import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
TESTS_DIR = REPO_ROOT / "tests"

# Canary: file test co tren main tu vong m2. CI chay file test cua mot
# nhanh rieng le (sandbox) chi copy pyproject + file test do, khong co
# day du tests/ tren main — khi do gate doi chieu phai SKIP chu khong
# duoc fail oan: thieu file khong phai do merge lam mat, ma do checkout
# khong day du. Gate chi co y nghia khi chay tren checkout main day du.
FULL_MAIN_CANARY = "test_systems_arch_m2.py"


def _is_full_main_checkout(tests_dir: Path = TESTS_DIR) -> bool:
    return (tests_dir / FULL_MAIN_CANARY).is_file()

# Pin duoc khoa trong pyproject.toml (phai khop file that).
EXPECTED_PROD = {
    "fastapi": "0.165.0",
    "pydantic": "2.14.4",
    "cryptography": "50.0.2",
    "httpx": "0.28.1",
    "uvicorn": "0.54.0",
}
EXPECTED_DEV = {"pytest": "9.2.0"}

# Fix da triage co file test tren main (kiem chung tu danh sach file main).
# Them fix moi: them mot dong o day CUNG VOI file test cua no.
TRIAGED_FIXES_REQUIRED = {
    "fix_alert_group_workspace_telegram": "test_fix_alert_group_workspace_telegram.py",
    "fix_api_cursor_offset": "test_fix_api_cursor_offset.py",
    "fix_api_import_delete_assign_audit": "test_fix_api_import_delete_assign_audit.py",
    "fix_audit_chain_thread_safety": "test_fix_audit_chain_thread_safety.py",
    "fix_browser_profile_missing": "test_fix_browser_profile_missing.py",
    "fix_contracts_version_stale_tests": "test_fix_contracts_version_stale_tests.py",
    "fix_docker_compose_companions": "test_fix_docker_compose_companions.py",
    "fix_docker_compose_unrunnable": "test_fix_docker_compose_unrunnable.py",
    "fix_facebook_token_thumbnail": "test_fix_facebook_token_thumbnail.py",
    "fix_frontend_backend_contract_mismatch": "test_fix_frontend_backend_contract_mismatch.py",
    "fix_frontend_missing_manifest": "test_fix_frontend_missing_manifest.py",
    "fix_golden_missing_youtube": "test_fix_golden_missing_youtube.py",
    "fix_idempotency_hourly_bucket": "test_fix_idempotency_hourly_bucket.py",
    "fix_import_dual_contracts": "test_fix_import_dual_contracts.py",
    "fix_ingestion_normalize_removed": "test_fix_ingestion_normalize_removed.py",
    "fix_missing_dependency_manifest": "test_fix_missing_dependency_manifest.py",
    "fix_missing_tests_critical_modules": "test_fix_missing_tests_critical_modules.py",
    "fix_pipeline_fail_closed_wiring": "test_fix_pipeline_fail_closed_wiring.py",
    "fix_proxy_pool_fail_closed": "test_fix_proxy_pool_fail_closed.py",
    "fix_scheduler_enqueue_fail_open": "test_fix_scheduler_enqueue_fail_open.py",
    "fix_scheduler_quota_proxy_per_account": "test_fix_scheduler_quota_proxy_per_account.py",
    "fix_scheduler_scheduled_at_lost": "test_fix_scheduler_scheduled_at_lost.py",
    "fix_test_portability_stub_contracts": "test_fix_test_portability_stub_contracts.py",
    "fix_tiktok_auth_fabricated": "test_fix_tiktok_auth_fabricated.py",
    "fix_tiktok_scope_test_auth": "test_fix_tiktok_scope_test_auth.py",
    "fix_tiktok_upload_scope": "test_fix_tiktok_upload_scope.py",
    "fix_timeseries_rollup_ddl": "test_fix_timeseries_rollup_ddl.py",
    "fix_token_lock_distributed": "test_fix_token_lock_distributed.py",
    "fix_vault_kek_fallback_wipe": "test_fix_vault_kek_fallback_wipe.py",
    "fix_youtube_quota_cache_channel": "test_fix_youtube_quota_cache_channel.py",
}

# Fix da nop nhung chua co file test tren main — dang xfail de suite van
# xanh; khi chung merge xong thi chuyen sang REQUIRED. (fix_unified_store_
# pipeline_wiring khong co o day: no da duoc thay the boi
# fix_pipeline_fail_closed_wiring va da merge.)
TRIAGED_FIXES_KNOWN_MISSING = {
    "fix_core_store_return_type": "test_fix_core_store_return_type.py",
    "fix_import_dual_contracts_residual": "test_fix_import_dual_contracts_residual.py",
    "fix_monitor_golden_killswitch_prometheus": "test_fix_monitor_golden_killswitch_prometheus.py",
}


def _load_pyproject() -> dict:
    assert PYPROJECT.is_file(), "thieu pyproject.toml o repo root"
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _parse_deps(deps: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for dep in deps:
        m = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9.+-]*)", dep.strip())
        assert m, f"dependency chua pin chinh xac (==x.y.z): {dep!r}"
        out[m.group(1).lower()] = m.group(2)
    return out


def test_pyproject_exists_and_parses():
    data = _load_pyproject()
    assert "project" in data
    assert data["project"]["name"] == "multi-account-video-stats"


def test_requires_python_at_least_311():
    data = _load_pyproject()
    req = data["project"]["requires-python"]
    assert "3.11" in req, f"requires-python phai >=3.11, got {req!r}"
    assert sys.version_info >= (3, 11)


def test_production_dependencies_pinned():
    data = _load_pyproject()
    deps = _parse_deps(data["project"]["dependencies"])
    for name, version in EXPECTED_PROD.items():
        assert deps.get(name) == version, (
            f"thieu/sai pin production {name}: expected =={version}, got {deps.get(name)!r}"
        )


def test_cryptography_is_production_not_optional():
    """cryptography phai nam trong project.dependencies, khong chi dev/optional.

    Neu no chi o dev hoac optional, moi truong production khong cai no se
    khien vault roi lang le ve fallback stdlib ma khong ai phat hien.
    """
    data = _load_pyproject()
    prod = _parse_deps(data["project"]["dependencies"])
    assert "cryptography" in prod
    assert prod["cryptography"] == EXPECTED_PROD["cryptography"]


def test_dev_dependencies_pinned():
    data = _load_pyproject()
    dev = data["project"].get("optional-dependencies", {}).get("dev", [])
    parsed = _parse_deps(dev)
    for name, version in EXPECTED_DEV.items():
        assert parsed.get(name) == version, (
            f"thieu/sai pin dev {name}: expected =={version}, got {parsed.get(name)!r}"
        )


def test_pyproject_is_the_single_manifest():
    """Chi co pyproject.toml; khong them requirements.txt song song de
    hai manifest lech nhau."""
    assert not (REPO_ROOT / "requirements.txt").is_file()
    assert not (REPO_ROOT / "requirements-dev.txt").is_file()


def test_pytest_pythonpath_puts_repo_root_on_sys_path():
    """Khong can tests/conftest.py rieng: pythonpath=["."] trong
    pyproject dat repo root len sys.path de `import src.*` chay duoc."""
    opts = _load_pyproject().get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert "." in opts.get("pythonpath", [])


def test_no_local_venv_or_tmp_path_in_manifest():
    text = PYPROJECT.read_text(encoding="utf-8")
    for forbidden in (".venv", "/tmp/m2", "C:\\Users", "file://"):
        assert forbidden not in text, f"manifest chua duong dan local: {forbidden!r}"


def test_installed_versions_match_pins():
    for name, version in {**EXPECTED_PROD, **EXPECTED_DEV}.items():
        installed = metadata.version(name)
        assert installed == version, (
            f"{name} da cai {installed} != pin =={version}; "
            "cai lai tu manifest (pip install .[dev]) thay vi dung .venv local"
        )


@pytest.mark.parametrize("name", ["fastapi", "pydantic", "httpx", "uvicorn"])
def test_runtime_deps_importable(name):
    __import__(name)


def test_cryptography_aesgcm_available():
    """AES-256-GCM that phai ton tai — chung minh vault dung duoc ma hoa that."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = AESGCM.generate_key(bit_length=256)
    aes = AESGCM(key)
    nonce = b"\x00" * 12
    ct = aes.encrypt(nonce, b"manifest-check", None)
    assert aes.decrypt(nonce, ct, None) == b"manifest-check"


def test_full_main_checkout_detection(tmp_path):
    """Logic nhan dien checkout day du: co canary thi gate chay,
    khong co thi gate skip (sandbox chay le file test)."""
    assert _is_full_main_checkout(tmp_path) is False
    (tmp_path / FULL_MAIN_CANARY).write_text("# canary\n", encoding="utf-8")
    assert _is_full_main_checkout(tmp_path) is True


@pytest.mark.parametrize(
    "fix_code,test_file", sorted(TRIAGED_FIXES_REQUIRED.items())
)
def test_triaged_fix_test_file_present_on_main(fix_code, test_file):
    """Cong doi chieu merge: fix da triage ma mat file test tren main
    thi merge do coi nhu chua xong. Chi chay tren checkout main day du;
    o sandbox chay le file test (khong co canary) thi skip thay vi fail
    oan — thieu file o do khong phai do merge lam mat."""
    if not _is_full_main_checkout():
        pytest.skip("checkout khong day du (thieu canary tren main) — gate doi chieu khong ap dung")
    assert (TESTS_DIR / test_file).is_file(), (
        f"fix {fix_code} da triage nhung thieu file test "
        f"tests/{test_file} tren main (merge lam mat fix?)"
    )


@pytest.mark.parametrize(
    "fix_code,test_file",
    [pytest.param(code, f, marks=pytest.mark.xfail(
        reason=f"fix {code} da nop nhung chua merge file test",
        strict=False))
     for code, f in sorted(TRIAGED_FIXES_KNOWN_MISSING.items())],
)
def test_known_missing_fix_test_file(fix_code, test_file):
    """Theo doi cac fix da nop nhung chua co file test tren main.
    Khi mot fix merge xong, test nay tu pass — chuyen no sang REQUIRED."""
    if not _is_full_main_checkout():
        pytest.skip("checkout khong day du (thieu canary tren main) — gate doi chieu khong ap dung")
    assert (TESTS_DIR / test_file).is_file(), (
        f"fix {fix_code} van thieu file test tests/{test_file} tren main"
    )
