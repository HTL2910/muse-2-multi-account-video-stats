"""Regression tests for fix_frontend_backend_contract_mismatch.

The dashboard and the FastAPI backend (src/api/main.py, approach_13) had
drifted apart: the frontend called /api/* paths (backend mounts routes
at the root -> 404), never sent the mandatory workspace_id (-> 422),
read AccountOut fields the backend does not return (tags/avatar_url/
proxy{...} -> undefined.join TypeError), called endpoints that do not
exist (/analytics/overview, /export/*.xlsx, /jobs/{id}/retry,
/accounts/{id}/tags), opened an SSE EventSource while the backend only
offers WebSocket /ws/events, and POSTed assign-proxy with no body while
the backend requires {proxy_id}.

The single contract is the backend OpenAPI. These tests pin the
frontend to it statically: every api()/WebSocket path used in the .tsx
must be a real backend route, row interfaces must be subsets of the
real response schemas, and the removed behaviours must not return.

Run from repo root:  python -m pytest tests/test_fix_frontend_backend_contract_mismatch.py -q
Stdlib only.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[1]
TSX = REPO_ROOT / "src" / "web" / "pages" / "DashboardAccounts.tsx"

# Real routes of src/api/main.py (approach_13) — the contract's source of truth.
BACKEND_ROUTES = {
    "/health", "/auth/login",
    "/accounts", "/accounts/import",
    "/accounts/{}", "/accounts/{}/check-alive", "/accounts/{}/sync-videos",
    "/accounts/{}/assign-proxy",
    "/videos", "/videos/{}/metrics",
    "/proxies", "/proxies/health", "/proxies/import",
    "/groups", "/tags", "/jobs",
}
# Response schemas (fields) of the backend, from src/api/main.py.
ACCOUNT_OUT_FIELDS = {"id", "workspace_id", "platform", "platform_user_id",
                      "display_name", "status", "health_score",
                      "proxy_binding_id", "last_sync_at"}
VIDEO_ITEM_FIELDS = {"id", "account_id", "platform", "platform_video_id",
                     "title", "url", "published_at", "status"}
JOB_ITEM_FIELDS = {"id", "job_type", "account_id", "status", "created_at"}
PROXY_ITEM_FIELDS = {"id", "host", "port", "country", "status", "score"}


def _src() -> str:
    assert TSX.exists()
    return TSX.read_text(encoding="utf-8")


def _code(src: str) -> str:
    """Source with comments stripped — contract checks apply to code, not prose."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def _interface_fields(src: str, name: str) -> set[str]:
    m = re.search(rf"interface {name} \{{([^}}]*)\}}", src, re.S)
    assert m, f"interface {name} not found"
    return set(re.findall(r"(\w+)\??:\s", m.group(1)))


def _called_paths(src: str) -> set[str]:
    """Static path prefixes of every api(...) call, ${...} normalised to {}."""
    paths = set()
    for raw in re.findall(r"""api(?:<[^>]*>)?\(\s*`([^`]*)`""", src):
        path = raw.split("?")[0]
        path = re.sub(r"\$\{[^}]*\}", "{}", path)
        paths.add(path)
    for raw in re.findall(r"""api(?:<[^>]*>)?\(\s*"([^"]*)""", src):
        paths.add(raw.split("?")[0])
    return paths


def test_no_api_prefix_and_no_sse():
    src = _code(_src())
    assert '"/api' not in src and "`/api" not in src, "frontend must not use the /api prefix (backend routes are at root)"
    assert "EventSource" not in src, "backend has no SSE endpoint; EventSource must not be used"
    assert "new WebSocket" in src and "/ws/events" in src, "realtime must use the backend WebSocket /ws/events"


def test_every_called_path_is_a_real_backend_route():
    code = _code(_src())
    called = _called_paths(code)
    assert called, "no api() calls found — test harness broken?"
    # The bulk-action template /accounts/${id}/${action} is dynamic: expand
    # it with the literal actions passed to bulk.mutate(...) in the source.
    if "/accounts/{}/{}" in called:
        called.discard("/accounts/{}/{}")
        actions = set(re.findall(r'mutate\("([^"]+)"\)', code))
        assert actions, "bulk actions not found in source"
        called |= {f"/accounts/{{}}/{a}" for a in actions}
    unknown = {p for p in called if p not in BACKEND_ROUTES}
    assert not unknown, f"frontend calls routes the backend does not have: {unknown}"


def test_removed_endpoints_are_not_called():
    src = _code(_src())
    for gone in ("/analytics/overview", "/export/", "/retry", "/accounts/${id}/tags"):
        assert gone not in src, f"frontend still references non-existent endpoint fragment: {gone}"


def test_accounts_query_sends_workspace_id_and_omits_empty_filters():
    src = _src()
    assert "workspace_id" in src and "currentWorkspaceId" in src
    assert 'params.set("platform", platform)' in src and 'params.set("status", status)' in src, (
        "filters must be set conditionally (empty enum values are rejected with 422)")


def test_assign_proxy_sends_proxy_id_body():
    src = _src()
    assert "proxy_id" in src and "JSON.stringify({ proxy_id" in src, (
        "assign-proxy must POST a {proxy_id} body as the backend requires")


def test_row_interfaces_match_backend_schemas():
    src = _src()
    assert _interface_fields(src, "AccountRow") <= ACCOUNT_OUT_FIELDS, (
        f"AccountRow reads fields AccountOut does not return: "
        f"{_interface_fields(src, 'AccountRow') - ACCOUNT_OUT_FIELDS}")
    assert _interface_fields(src, "VideoRow") <= VIDEO_ITEM_FIELDS
    assert _interface_fields(src, "JobRow") <= JOB_ITEM_FIELDS
    assert _interface_fields(src, "ProxyRow") <= PROXY_ITEM_FIELDS
    # The exact expressions that used to crash on undefined:
    assert "a.tags" not in src and "a.avatar_url" not in src and "a.proxy." not in src
    assert "v.thumbnail_url" not in src and "j.attempts" not in src and "j.last_error" not in src


def test_metrics_page_shape_is_used_for_history():
    src = _src()
    assert "Page<MetricPoint>" in src and "history.data.items" in src, (
        "GET /videos/{id}/metrics returns a Page, not a bare array")


if __name__ == "__main__":
    test_no_api_prefix_and_no_sse()
    test_every_called_path_is_a_real_backend_route()
    test_removed_endpoints_are_not_called()
    test_accounts_query_sends_workspace_id_and_omits_empty_filters()
    test_assign_proxy_sends_proxy_id_body()
    test_row_interfaces_match_backend_schemas()
    test_metrics_page_shape_is_used_for_history()
    print("ALL TESTS PASSED")
