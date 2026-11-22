"""Regression test for fix_missing_tests_critical_modules.

Guards the fix itself: tests/test_backend_sec_m2.py must exist, must
import/exercise all five critical modules (vault, youtube_connector,
profile_manager, alert_rule_engine, collect_sample_data), and must
expose a meaningful number of test functions. If someone deletes or
guts that file later, this test fails instead of coverage silently
dropping back to zero.

Portable: repo root derived from this file's location.
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TARGET = REPO_ROOT / "tests" / "test_backend_sec_m2.py"

REQUIRED_MODULE_MARKERS = [
    "src.accounts.vault",
    "src.connectors.youtube_connector",
    "src.browser.profile_manager",
    "src.api.alert_rule_engine",
    "collect_sample_data",
]

# One test per council-required behaviour area, minimum.
REQUIRED_AREAS = [
    "vault",        # store/rotate/restart/tamper + refresh race/revoked
    "youtube",      # quota / batch / analytics mapping
    "profile",      # fingerprint / proxy fail-closed / manual queue
    "alert",        # dedupe / sync-fail-3 / spike-drop
    "collect",      # golden cases + no real credentials
]

MIN_TESTS = 15


def test_backend_sec_m2_exists_and_covers_all_five_modules():
    assert TARGET.exists(), f"missing coverage file: {TARGET}"
    src = TARGET.read_text(encoding="utf-8")
    for marker in REQUIRED_MODULE_MARKERS:
        assert marker in src, f"test_backend_sec_m2.py does not cover {marker}"


def test_backend_sec_m2_has_tests_for_every_required_area():
    tree = ast.parse(TARGET.read_text(encoding="utf-8"))
    names = [n.name for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef) and n.name.startswith("test_")]
    assert len(names) >= MIN_TESTS, \
        f"only {len(names)} tests in test_backend_sec_m2.py: {names}"
    for area in REQUIRED_AREAS:
        assert any(area in n for n in names), \
            f"no test function covers area '{area}': {names}"


def test_backend_sec_m2_is_importable_and_collectable():
    spec = importlib.util.spec_from_file_location("test_backend_sec_m2", TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # imports the 5 modules under test
    fns = [n for n in dir(mod) if n.startswith("test_")
           and callable(getattr(mod, n))]
    assert len(fns) >= MIN_TESTS


if __name__ == "__main__":
    test_backend_sec_m2_exists_and_covers_all_five_modules()
    test_backend_sec_m2_has_tests_for_every_required_area()
    test_backend_sec_m2_is_importable_and_collectable()
    print("ALL TESTS PASSED")
