"""Regression tests for fix_test_portability_stub_contracts.

Guards the exact failure the council reported:
- tests/test_fullstack_ux_m2.py must not contain a hardcoded author-machine
  path ("/tmp/m2") or wildcard imports ever again;
- the test suite must run against the REAL src/contracts.py v1.1.0 of this
  repo — not a stub contracts module shadowing it on sys.path;
- PlatformAdapter must keep the locked v1.1.0 signatures.
Portable by construction: repo root comes from this file's location.
Run from repo root:  python -m pytest tests/test_fix_test_portability_stub_contracts.py -q

fix_contracts_version_stale_portability:
- The version assert below no longer hardcodes the contracts version.
  This suite was the one file the earlier fix_contracts_version_stale_tests
  missed: it kept asserting the pre-bump version and stayed red on the
  Team-Lead-locked 1.1.0 contract. The assert now consumes the ONE shared
  pin — EXPECTED_CONTRACTS_VERSION in tests/test_fullstack_ux_m2.py —
  extracted from that file's source (the same mechanism the stale-tests
  guard uses), so this suite carries no second version literal that can
  go stale. Convention: a PR that bumps CONTRACTS_VERSION updates
  EXPECTED_CONTRACTS_VERSION in tests/test_fullstack_ux_m2.py in the
  SAME PR, and this suite follows automatically.
"""
import inspect
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.contracts as contracts
from src.contracts import CONTRACTS_VERSION, PlatformAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
TARGET_TEST_FILE = REPO_ROOT / "tests" / "test_fullstack_ux_m2.py"


def _shared_expected_contracts_version() -> str:
    """Read the single-source version pin from the main suite.

    EXPECTED_CONTRACTS_VERSION in tests/test_fullstack_ux_m2.py is the
    one place the test suites pin the locked contracts version;
    extracting it (instead of importing the whole suite or restating
    the literal here) keeps exactly one editable copy in the repo's
    test tree.
    """
    source = TARGET_TEST_FILE.read_text(encoding="utf-8")
    m = re.search(r'EXPECTED_CONTRACTS_VERSION\s*=\s*"([^"]+)"', source)
    assert m is not None, (
        "EXPECTED_CONTRACTS_VERSION missing from "
        "tests/test_fullstack_ux_m2.py — the shared version pin is gone")
    return m.group(1)


EXPECTED_CONTRACTS_VERSION = _shared_expected_contracts_version()


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
    assert CONTRACTS_VERSION == EXPECTED_CONTRACTS_VERSION


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
