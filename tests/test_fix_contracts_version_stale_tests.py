"""Regression test for fix_contracts_version_stale_tests.

The bug was in the TESTS, not the contract: CONTRACTS_VERSION was
bumped 1.0.0 -> 1.1.0 by the Team-Lead-locked fix_idempotency_
hourly_bucket (hourly snapshot_key, CollectJob key by scheduled
minute), but two suites kept asserting the old version and turned CI
red on a correct, approved change.

This guard pins the CURRENT locked version in exactly one place here
and checks the main suite consumes its own single-source constant
instead of an inline literal, so the next approved bump fails in
exactly one obvious spot rather than hunting stale pins.

Convention (council): any PR that bumps CONTRACTS_VERSION must update
the version pins in the same PR.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import src.contracts as contracts  # noqa: E402
from src.contracts import CONTRACTS_VERSION  # noqa: E402

# The one place this fix pins the locked version. Bump together with
# src/contracts.py and EXPECTED_CONTRACTS_VERSION in
# tests/test_fullstack_ux_m2.py — in the SAME PR.
LOCKED_VERSION = "1.1.0"
STALE_VERSION = "1." + "0.0"  # concatenated: this guard must not self-match


def test_contracts_version_is_the_locked_1_1_0():
    assert Path(contracts.__file__).resolve() == \
        (REPO_ROOT / "src" / "contracts.py").resolve()
    assert CONTRACTS_VERSION == LOCKED_VERSION
    # The bump must be the documented, Team-Lead-locked one — the
    # changelog entry in contracts.py is the lock evidence.
    source = Path(contracts.__file__).read_text(encoding="utf-8")
    assert f"Changelog {LOCKED_VERSION}" in source
    assert "fix_idempotency_hourly_bucket" in source


def test_fullstack_suite_uses_single_source_constant_not_stale_pin():
    target = REPO_ROOT / "tests" / "test_fullstack_ux_m2.py"
    source = target.read_text(encoding="utf-8")
    # No stale assertion against the pre-bump version anywhere in the file.
    assert f'CONTRACTS_VERSION == "{STALE_VERSION}"' not in source
    assert f"contracts v{STALE_VERSION}" not in source
    # The suite defines the single-source constant at the locked value
    # and its lock test asserts against that constant.
    m = re.search(r'EXPECTED_CONTRACTS_VERSION\s*=\s*"([^"]+)"', source)
    assert m is not None, "EXPECTED_CONTRACTS_VERSION constant missing"
    assert m.group(1) == LOCKED_VERSION
    assert "CONTRACTS_VERSION == EXPECTED_CONTRACTS_VERSION" in source


if __name__ == "__main__":
    test_contracts_version_is_the_locked_1_1_0()
    test_fullstack_suite_uses_single_source_constant_not_stale_pin()
    print("ALL TESTS PASSED")
