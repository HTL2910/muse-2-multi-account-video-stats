"""fix_missing_dependency_manifest — manifest phu thuoc tai lap duoc.

Kiem tra pyproject.toml la manifest duy nhat, pin day du, Python >= 3.11,
cryptography la dependency production (khong phai dev-only / optional) de
vault khong bao gio roi lang le ve fallback stdlib, va cac dependency da
cai dung khop pin (ke ca AES-256-GCM that).

Chi dung stdlib + pytest; khong import bat ky module src nao.
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

# Pin duoc khoa trong pyproject.toml (phai khop file that).
EXPECTED_PROD = {
    "fastapi": "0.165.0",
    "pydantic": "2.14.4",
    "cryptography": "50.0.2",
    "httpx": "0.28.1",
    "uvicorn": "0.54.0",
}
EXPECTED_DEV = {"pytest": "9.2.0"}


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
