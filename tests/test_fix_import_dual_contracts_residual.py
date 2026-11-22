"""Guard test for fix_import_dual_contracts_residual (mechanical PR
across src/: remove every try/except ImportError fallback for the
contract and keep only the single ``from src.contracts import ...``
style, run from the repo root).

Why the guard is AST-based: a naive text grep for the banned import
spellings false-positives on docstrings and comments that merely
DESCRIBE the fallback (the earlier core-models guard hit exactly
that trap on its own file). ``ast`` sees only real import statements,
so prose mentions are ignored — see
test_guard_ignores_docstring_mentions.

Inventory (verified by direct read of main, 2026-11-22):
- EXPECTED_CLEAN — hard-asserted clean. Files move here from
  KNOWN_REMAINING as each follow-up branch lands; a file here with
  a fallback fails the suite.
- KNOWN_REMAINING — still carry a fallback on main; xfail(strict=False)
  so they stay green until their own branch fixes them. XPASS when a
  later branch fixes one is expected and harmless.
- test_no_new_fallback_files — the whole-tree guard: any src/**/*.py
  file with a banned import that is NOT already in KNOWN_REMAINING
  fails, so a new fallback (or an inventory miss) cannot slip in.

Scope: only the contract import (bare ``contracts`` at any relative
level, and the bare ``account_model`` fallback in risk/account_health,
which points at a top-level module that does not exist). Relative
fallbacks for OTHER modules (pipeline.py's ``..core.models``) are a
separate follow-up and are intentionally NOT flagged here.
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

SRC_DIR = REPO_ROOT / "src"

# Files verified clean by direct read of main (no fallback import).
EXPECTED_CLEAN = [
    "src/ai/recommendation_engine.py",   # this branch
    "src/core/models.py",                # fixed in fix_import_dual_contracts
    "src/accounts/account_model.py",     # fixed in fix_import_dual_contracts
    "src/ingestion/pipeline.py",         # contracts import is direct
    "src/worker/__main__.py",            # contracts import is direct
    "src/proxy/proxy_pool_manager.py",   # contracts import is direct
    "src/security/rbac_audit.py",        # contracts import is direct
    "src/scheduler/__main__.py",         # contracts import is direct
]

# Files verified by direct read to STILL carry a fallback import.
# Each gets its own fix branch, then moves to EXPECTED_CLEAN.
KNOWN_REMAINING = [
    "src/analytics/time_series.py",
    "src/browser/profile_manager.py",
    "src/observability/monitor.py",
    "src/risk/account_health.py",        # also: bare 'account_model' fallback
    "src/connectors/facebook_connector.py",
    "src/scheduler/task_scheduler.py",
    "src/connectors/tiktok_connector.py",
    "src/api/main.py",                   # top-level AND nested in _enqueue
    "src/api/alert_rule_engine.py",
    "src/accounts/vault.py",
    "src/connectors/youtube_connector.py",
]


def _banned_imports(path: Path) -> list[str]:
    """All banned contract-import statements in one file (AST).

    A banned import is any ``ImportFrom`` whose module is exactly
    ``contracts`` (bare or relative — only ``src.contracts`` is legal),
    any bare ``import contracts``, and any bare ``from account_model
    import`` (no such top-level module exists). Returns human-readable
    ``line N: ...`` hits; empty means clean.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "contracts":
                dots = "." * node.level
                hits.append(f"line {node.lineno}: from {dots}contracts import ...")
            elif node.module == "account_model" and node.level == 0:
                hits.append(f"line {node.lineno}: from account_model import ...")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("contracts", "account_model"):
                    hits.append(f"line {node.lineno}: import {alias.name}")
    return hits


def _contracts_imports(path: Path) -> list[str]:
    """Every ImportFrom that touches a module named '*contracts*'."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
            and node.module.endswith("contracts")]


def _assert_single_import_style(rel_path: str) -> None:
    path = REPO_ROOT / rel_path
    assert path.is_file(), f"{rel_path} is missing from this checkout"
    hits = _banned_imports(path)
    assert not hits, f"{rel_path} still has a fallback import: {hits}"
    mods = _contracts_imports(path)
    assert all(m == "src.contracts" for m in mods), (
        f"{rel_path}: contract imports must all be 'src.contracts', got {mods}")


@pytest.mark.parametrize("rel_path", EXPECTED_CLEAN)
def test_expected_clean_files(rel_path):
    """Files already fixed: no fallback import, only src.contracts."""
    _assert_single_import_style(rel_path)


@pytest.mark.xfail(strict=False, reason="fallback import still present — "
                  "fixed in its own branch, then moved to EXPECTED_CLEAN")
@pytest.mark.parametrize("rel_path", KNOWN_REMAINING)
def test_known_remaining_files(rel_path):
    """Tracked leftovers: each must turn green in its own branch."""
    path = REPO_ROOT / rel_path
    if not path.is_file():
        pytest.skip(f"{rel_path} is not present in this checkout")
    _assert_single_import_style(rel_path)


def _posix_rel(path: Path) -> str:
    """Relative path with '/' separators on every OS.

    CI runs on Windows, where str() of a Path uses '\\' — the
    un-normalised string never matched the '/'-style inventory, so
    every tracked file looked "new" and the guard failed on Windows
    while passing on Linux (CI round 1: 1 failed, 11 passed,
    11 xfailed). ``as_posix()`` is the fix.
    """
    return path.relative_to(REPO_ROOT).as_posix()


def test_windows_style_paths_match_inventory():
    """Regression for the Windows separator bug: a Windows-style
    relative path must normalise to exactly the inventory spelling."""
    from pathlib import PureWindowsPath
    assert PureWindowsPath("src\\accounts\\vault.py").as_posix() == "src/accounts/vault.py"
    for rel in EXPECTED_CLEAN + KNOWN_REMAINING:
        assert PureWindowsPath(rel.replace("/", "\\")).as_posix() == rel


def test_no_new_fallback_files():
    """Whole-tree guard: no src/**/*.py may carry a banned import
    unless it is already tracked in KNOWN_REMAINING."""
    found: dict[str, list[str]] = {}
    for path in sorted(SRC_DIR.rglob("*.py")):
        hits = _banned_imports(path)
        if hits:
            found[_posix_rel(path)] = hits
    unexpected = sorted(set(found) - set(KNOWN_REMAINING))
    assert not unexpected, (
        "New file(s) with a fallback import — fix them or inventory them "
        f"in KNOWN_REMAINING: {[(f, found[f]) for f in unexpected]}")


def test_guard_ignores_docstring_mentions():
    """Regression: this very test file's docstring DESCRIBES the banned
    import spellings; the AST detector must not flag its own file."""
    assert _banned_imports(Path(__file__)) == []
    # A file with a docstring mention + a legal import is clean too.
    probe = ("\"\"\"Mentions the old fallback in prose only.\"\"\"\n"
             "from src.contracts import Platform\n")
    assert _banned_imports_from_source(probe) == []


def _banned_imports_from_source(source: str) -> list[str]:
    tree = ast.parse(source)
    return [f"line {n.lineno}" for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and n.module == "contracts"]


def test_target_module_single_identity():
    """Smoke: the fixed engine imports from repo root, shares one
    module object with src.contracts, and never loads bare 'contracts'."""
    import src.ai.recommendation_engine as engine
    import src.contracts as contracts

    assert "contracts" not in sys.modules
    assert engine.Platform is contracts.Platform
    assert engine.Video is contracts.Video
    assert engine.UnifiedVideoMetric is contracts.UnifiedVideoMetric
    assert engine.ViewForecast is contracts.ViewForecast
    assert engine.Recommendation is contracts.Recommendation
    assert engine.compute_engagement_rate is contracts.compute_engagement_rate
    assert engine.utcnow is contracts.utcnow


def test_engine_functional_smoke():
    """Functional smoke of the fixed module (no logic changed)."""
    import src.ai.recommendation_engine as engine
    import src.contracts as contracts

    t0 = datetime(2026, 11, 22, 10, 0, tzinfo=timezone.utc)
    videos = [
        contracts.Video(id="v1", account_id="a1", platform=contracts.Platform.YOUTUBE,
                        platform_video_id="yt1", title="Meo tam su dem khuya",
                        description="meo ke chuyen dem khuya",
                        published_at=t0.replace(hour=21)),
        contracts.Video(id="v2", account_id="a1", platform=contracts.Platform.YOUTUBE,
                        platform_video_id="yt2", title="Meo tam su ban ngay",
                        description="meo ke chuyen ban ngay",
                        published_at=t0.replace(hour=9)),
        contracts.Video(id="v3", account_id="a1", platform=contracts.Platform.YOUTUBE,
                        platform_video_id="yt3", title="Cho chay bo cong vien",
                        description="cho chay bo cong vien buoi sang",
                        published_at=t0.replace(hour=7)),
    ]
    metrics = {
        v.id: contracts.UnifiedVideoMetric(
            video_id=v.id, account_id="a1", platform=contracts.Platform.YOUTUBE,
            platform_video_id=v.platform_video_id,
            views=1000 + 100 * i, likes=100 + 10 * i,
            comments=15 + i, shares=8 + i, collected_at=t0)
        for i, v in enumerate(videos)
    }

    assert engine.tokenize("Meo Tam Su Dem Khuya") == ["meo", "tam", "su", "dem", "khuya"]
    vecs, vocab = engine.tfidf_vectors([v.title for v in videos])
    assert len(vecs) == 3 and vocab
    labels, _ = engine.kmeans([[1.0, 0.0], [1.1, 0.1], [0.0, 1.0]], 2)
    assert labels[0] == labels[1] and labels[2] != labels[0]

    clusters = engine.cluster_videos(videos, metrics, k=2)
    assert clusters and {v for c in clusters for v in c.video_ids} == {"v1", "v2", "v3"}

    hist = [contracts.UnifiedVideoMetric(
        video_id="v1", account_id="a1", platform=contracts.Platform.YOUTUBE,
        platform_video_id="yt1", views=100 * (i + 1), likes=10 * (i + 1),
        comments=i + 1, shares=i, collected_at=t0 + timedelta(hours=i))
        for i in range(4)]
    fc = engine.forecast_views(hist, horizon="24h", video_id="v1")
    assert fc.predicted_views >= hist[-1].views and 0.1 <= fc.confidence <= 0.9

    hours = engine.best_posting_hours(videos, metrics, top_n=2)
    assert len(hours) == 2 and all(0 <= h < 24 for h in hours)

    recs = engine.recommend(account_id="a1", videos=videos, metrics=metrics, top_n=3)
    assert recs and all(isinstance(r, contracts.Recommendation) for r in recs)
    assert recs[0].account_id == "a1"
