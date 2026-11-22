"""Regression tests for fix_frontend_missing_manifest.

src/web/pages/DashboardAccounts.tsx imports react and
@tanstack/react-query and is documented as Next.js + TypeScript, but the
repo had NO package.json / tsconfig / lockfile anywhere, so the frontend
could not be installed, typechecked or built from a fresh checkout.
The fix adds src/web/package.json (src/web is the Next.js project root;
pages/ is its Pages Router directory):

- dependencies cover every bare import used by the .tsx sources, plus
  react-dom and next (the documented stack);
- devDependencies carry typescript + React/Node types;
- scripts provide dev/build/start and a typecheck that passes all
  compiler options as CLI flags, so it runs WITHOUT a tsconfig.json
  (Next itself needs no next.config — it runs on defaults and generates
  tsconfig.json on first dev/build);
- engines pins Node >= 20 (Next 15 requirement).

CI equivalent of this file's checks plus `npm run typecheck`:
    cd src/web && npm install && npm run typecheck

Run from repo root:  python -m pytest tests/test_fix_frontend_missing_manifest.py -q
Stdlib only.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_DIR = REPO_ROOT / "src" / "web"
MANIFEST = WEB_DIR / "package.json"
TSX_SOURCES = sorted((WEB_DIR / "pages").glob("*.tsx"))


def _manifest() -> dict:
    assert MANIFEST.exists(), "src/web/package.json is missing"
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _bare_imports() -> set[str]:
    """Package names imported by the .tsx sources (relative imports excluded)."""
    names: set[str] = set()
    pattern = re.compile(r"""from\s+["']([^"']+)["']""")
    for src in TSX_SOURCES:
        for spec in pattern.findall(src.read_text(encoding="utf-8")):
            if spec.startswith("."):
                continue
            names.add("/".join(spec.split("/")[:2]) if spec.startswith("@")
                      else spec.split("/")[0])
    return names


def test_manifest_is_valid_and_private():
    pkg = _manifest()
    assert pkg.get("name"), "package.json needs a name"
    assert pkg.get("private") is True, "app package must be private (not published to npm)"
    assert pkg.get("version"), "package.json needs a version"


def test_every_tsx_import_is_declared():
    assert TSX_SOURCES, "no .tsx sources found under src/web/pages"
    pkg = _manifest()
    declared = set(pkg.get("dependencies", {})) | set(pkg.get("devDependencies", {}))
    missing = _bare_imports() - declared
    assert not missing, f"imports used by the dashboard but not declared: {missing}"


def test_documented_stack_is_declared():
    pkg = _manifest()
    deps = pkg.get("dependencies", {})
    for name in ("react", "react-dom", "next", "@tanstack/react-query"):
        assert name in deps, f"{name} (documented Next.js/React stack) missing from dependencies"
    dev = pkg.get("devDependencies", {})
    for name in ("typescript", "@types/react", "@types/react-dom", "@types/node"):
        assert name in dev, f"{name} missing from devDependencies"
    # Versions must be real semver ranges, not placeholders.
    for name, spec in list(deps.items()) + list(dev.items()):
        assert re.match(r"^[\^~>=<\d]", str(spec)), f"{name}: suspicious version spec {spec!r}"


def test_scripts_cover_dev_build_and_typecheck():
    scripts = _manifest().get("scripts", {})
    assert scripts.get("dev", "").startswith("next dev")
    assert scripts.get("build", "").startswith("next build")
    assert scripts.get("start", "").startswith("next start")
    typecheck = scripts.get("typecheck", "")
    assert typecheck.startswith("tsc") and "--noEmit" in typecheck, (
        f"typecheck must run tsc --noEmit, got {typecheck!r}")
    assert "--strict" in typecheck, "typecheck must be strict (matches the TS contract of approach_12)"
    # Self-contained: no tsconfig file is referenced, options ride on the CLI.
    assert "tsconfig" not in typecheck


def test_node_engine_matches_next_requirement():
    engines = _manifest().get("engines", {})
    assert "node" in engines and "20" in engines["node"], (
        f"engines.node must require Node >= 20 for Next 15, got {engines}")


if __name__ == "__main__":
    test_manifest_is_valid_and_private()
    test_every_tsx_import_is_declared()
    test_documented_stack_is_declared()
    test_scripts_cover_dev_build_and_typecheck()
    test_node_engine_matches_next_requirement()
    print("ALL TESTS PASSED")
