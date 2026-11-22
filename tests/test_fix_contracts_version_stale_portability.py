"""Regression guard for fix_contracts_version_stale_portability.

The portability suite (tests/test_fix_test_portability_stub_contracts.py)
was the one version-locking file the earlier
fix_contracts_version_stale_tests missed: it kept asserting the
pre-bump contracts version and stayed red after the Team-Lead-locked
bump. This guard makes that miss unrepeatable:

- the portability suite carries NO version literal of its own — its
  lock assert compares CONTRACTS_VERSION against the shared
  EXPECTED_CONTRACTS_VERSION pin defined once in
  tests/test_fullstack_ux_m2.py, and that pin is the version the real
  contract reports;
- the portability suite's docstring no longer claims the old version
  and does claim the current one;
- an explicit inventory of every version-locking test file is scanned
  for a stale pin, so the next bump either updates the single shared
  pin or fails here with the offending file named. Any NEW suite that
  locks the contracts version MUST be added to VERSION_LOCK_FILES.

The stale literal is built by concatenation so this guard never
matches its own source.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.contracts import CONTRACTS_VERSION  # noqa: E402

STALE_VERSION = "1." + "0.0"  # pre-bump pin; concatenated, never literal

PORTABILITY_FILE = REPO_ROOT / "tests" / "test_fix_test_portability_stub_contracts.py"
MAIN_SUITE_FILE = REPO_ROOT / "tests" / "test_fullstack_ux_m2.py"

# Inventory: every test file that locks the contracts version. The
# portability suite was missing from the previous fix's inventory —
# it is listed here now, next to the suites that lock the version
# directly, plus this guard itself.
VERSION_LOCK_FILES = [
    MAIN_SUITE_FILE,
    PORTABILITY_FILE,
    REPO_ROOT / "tests" / "test_fix_contracts_version_stale_tests.py",
    Path(__file__).resolve(),
]


def _shared_expected_version() -> str:
    source = MAIN_SUITE_FILE.read_text(encoding="utf-8")
    m = re.search(r'EXPECTED_CONTRACTS_VERSION\s*=\s*"([^"]+)"', source)
    assert m is not None, "EXPECTED_CONTRACTS_VERSION missing from the main suite"
    return m.group(1)


def test_portability_suite_consumes_the_shared_pin():
    source = PORTABILITY_FILE.read_text(encoding="utf-8")
    # Its lock assert compares against the shared constant, which the
    # suite derives from the main suite's single-source definition.
    assert "CONTRACTS_VERSION == EXPECTED_CONTRACTS_VERSION" in source
    assert "_shared_expected_contracts_version" in source
    # No inline version literal is asserted anywhere in the file.
    assert not re.search(r'CONTRACTS_VERSION\s*==\s*"', source), (
        "portability suite asserts CONTRACTS_VERSION against an inline "
        "literal again — use the shared EXPECTED_CONTRACTS_VERSION pin")
    # And the shared pin is the version the real contract reports.
    assert CONTRACTS_VERSION == _shared_expected_version()


def test_no_stale_version_pin_in_any_version_locking_file():
    stale_assert = 'CONTRACTS_VERSION == "' + STALE_VERSION + '"'
    stale_doc_long = "contracts.py v" + STALE_VERSION
    stale_doc_short = "contracts v" + STALE_VERSION
    for path in VERSION_LOCK_FILES:
        source = path.read_text(encoding="utf-8")
        assert stale_assert not in source, (
            f"{path.name} still pins the stale contracts version")
        assert stale_doc_long not in source, (
            f"{path.name} docstring still claims the stale contracts version")
        assert stale_doc_short not in source, (
            f"{path.name} docstring still claims the stale contracts version")


def test_portability_docstring_claims_current_version():
    source = PORTABILITY_FILE.read_text(encoding="utf-8")
    current = _shared_expected_version()
    assert f"src/contracts.py v{current}" in source


if __name__ == "__main__":
    test_portability_suite_consumes_the_shared_pin()
    test_no_stale_version_pin_in_any_version_locking_file()
    test_portability_docstring_claims_current_version()
    print("ALL TESTS PASSED")
