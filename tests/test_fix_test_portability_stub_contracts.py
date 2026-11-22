"""Regression tests for fix_test_portability_stub_contracts.

Guards the exact failure the council reported:
- tests/test_fullstack_ux_m2.py must not contain a hardcoded author-machine
  path ("/tmp/m2") or wildcard imports ever again;
- the test suite must run against the REAL src/contracts.py v1.0.0 of this
  repo — not a stub contracts module shadowing it on sys.path;
- PlatformAdapter must keep the locked v1.0.0 signatures.
Portable by construction: repo root comes from this file's location.
Run from repo root:  python -m pytest tests/test_fix_test_portability_stub_contracts.py -q
"""
import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.contracts as contracts
from src.contracts import CONTRACTS_VERSION, PlatformAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_TEST_FILE = REPO_ROOT / "tests" / "test_fullstack_ux_m2.py"


def test_target_test_file_has_no_hardcoded_path_or_wildcard_import():
    source = TARGET_TEST_FILE.read_text(encoding="utf-8")
    assert "/tmp/m2" not in source, "hardcoded author-machine path returned"
    assert 'sys.path.insert(0, "/tmp' not in source
    assert "import *" not in source, "wildcard import returned"
    # Portability mechanism is present instead: root derived from __file__.
    assert "Path(__file__).resolve().parents[1]" in source


def test_contracts_module_is_the_real_repo_file():
    contracts_file = Path(contracts.__file__).resolve()
    assert contracts_file == (REPO_ROOT / "src" / "contracts.py").resolve(), (
        f"contracts imported from {contracts_file}, not the repo's src/contracts.py "
        "— a stub contracts is shadowing the locked contract")
    assert CONTRACTS_VERSION == "1.0.0"


def test_platform_adapter_signatures_locked():
    expected = {
        "authenticate": ["self", "account"],
        "check_alive": ["self", "account"],
        "list_videos": ["self", "account", "limit"],
        "get_video_stats": ["self", "account", "video"],
        "refresh_token": ["self", "account", "tokens"],
    }
    for method, params in expected.items():
        fn = getattr(PlatformAdapter, method, None)
        assert fn is not None, f"PlatformAdapter.{method} missing"
        assert list(inspect.signature(fn).parameters) == params


def test_spike_case_present_in_target_suite():
    # The council fix also required a REAL spike case in the pipeline test:
    # it must exercise a >10x and >100k jump and assert nothing was written.
    source = TARGET_TEST_FILE.read_text(encoding="utf-8")
    assert "spike" in source
    assert "2_000_000" in source
    assert "needs_review == 1" in source
    assert "metrics_written == 0" in source


if __name__ == "__main__":
    test_target_test_file_has_no_hardcoded_path_or_wildcard_import()
    test_contracts_module_is_the_real_repo_file()
    test_platform_adapter_signatures_locked()
    test_spike_case_present_in_target_suite()
    print("ALL TESTS PASSED")
