"""Regression tests for fix_tiktok_upload_scope.

The system is READ-ONLY (it only reads video statistics), but the TikTok
connector used to require the WRITE scope built below as UPLOAD_SCOPE —
it lets the app post videos as the user, violates least privilege, and
blocks OAuth app review for a stats-only product. The fix removes it:
REQUIRED_SCOPES keeps only user.info.basic + video.list, read insights
stay in OPTIONAL_SCOPES, and the module docstring states the read-only
contract (no posting / liking / commenting).

(The scope literal is assembled by concatenation so this test file does
not itself contain the string it forbids.)

Fix ``fix_tiktok_scope_test_auth``: the old
``test_authenticate_token_carries_only_read_scopes`` called
``connector.authenticate(account)`` with no auth_code, no TokenSet and
no token_provider, and expected a token back. Since
``fix_tiktok_auth_fabricated`` that call MUST raise ``TikTokAuthError``
('no_token_source') — authenticate() never fabricates a token. The
test was wrong, not the connector, and loosening authenticate() to
make the old test green would re-open the fabricated-token hole.
The test now supplies a REAL token (``tokens=`` or a Vault-backed
``token_provider``) and a fake transport that only accepts that
token's Bearer on /user/info/, then asserts the returned token keeps
exactly the read-only scopes.

Run from repo root:  python -m pytest tests/test_fix_tiktok_upload_scope.py -q
"""
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.connectors.tiktok_connector as tt
from src.contracts import Platform, PlatformAccount, TokenSet, utcnow

UPLOAD_SCOPE = "video." + "upload"
PUBLISH_SCOPE = "video." + "publish"
WRITE_SCOPES = {UPLOAD_SCOPE, PUBLISH_SCOPE}
READ_SCOPES = ["user.info.basic", "video.list"]
# Token that the fake transport accepts as a real TikTok token.
_REAL_TOKEN = "tok-read-only"


def test_required_scopes_are_read_only_and_exact():
    assert tt.REQUIRED_SCOPES == ["user.info.basic", "video.list"], (
        f"REQUIRED_SCOPES must be exactly the read-only set, got {tt.REQUIRED_SCOPES}")
    assert UPLOAD_SCOPE not in tt.REQUIRED_SCOPES


def test_no_write_scope_in_any_scope_list():
    all_scopes = list(tt.REQUIRED_SCOPES) + list(tt.OPTIONAL_SCOPES)
    assert not WRITE_SCOPES.intersection(all_scopes), f"write scope leaked into {all_scopes}"
    assert not any("upload" in s or "publish" in s for s in all_scopes)
    # Optional scopes, when present, are the read insights ones only.
    assert set(tt.OPTIONAL_SCOPES) <= {"user.info.stats", "video.insights"}


def test_docstring_states_read_only_contract():
    doc = tt.__doc__ or ""
    assert "READ-ONLY" in doc
    assert "never posts, likes or comments" in doc


class _ReadOnlyTokenHttp:
    """Fake TikTok transport: GET /user/info/ only accepts the real
    Bearer token under test; anything else is 401 access_token_invalid
    (mirrors the FakeTikTok in tests/test_fix_tiktok_auth_fabricated.py)."""

    def __init__(self):
        self.gets: list[dict] = []

    def get(self, url, headers, params):
        self.gets.append({"url": url, "headers": dict(headers)})
        if "/user/info/" in url:
            if headers.get("Authorization") != f"Bearer {_REAL_TOKEN}":
                return 401, {"error": {"code": "access_token_invalid",
                                       "message": "no/invalid token"}}
            return 200, {"data": {"open_id": "u1", "display_name": "Test"}}
        return 200, {"data": {}}


def _account():
    return PlatformAccount(id="a1", workspace_id="w1", platform=Platform.TIKTOK,
                           platform_user_id="u1", credential_ref="vault:tiktok/a1")


def _read_only_tokens() -> TokenSet:
    return TokenSet(access_token=_REAL_TOKEN, refresh_token="rt-read-only",
                    expires_at=utcnow() + timedelta(hours=1),
                    scopes=list(READ_SCOPES))


def test_authenticate_token_carries_only_read_scopes():
    # tokens= : caller supplies a REAL TokenSet (e.g. just exchanged in
    # the Login Kit step); authenticate() validates it live, then
    # returns it unchanged — scopes must stay exactly the read-only set.
    http = _ReadOnlyTokenHttp()
    connector = tt.TikTokConnector(http=http, jitter_seconds=(0.0, 0.0))
    tokens = connector.authenticate(_account(), tokens=_read_only_tokens())
    assert tokens.scopes == ["user.info.basic", "video.list"]
    assert UPLOAD_SCOPE not in tokens.scopes
    assert PUBLISH_SCOPE not in tokens.scopes
    # Live validation really happened, with THIS token's Bearer header.
    assert any(g["headers"].get("Authorization") == f"Bearer {_REAL_TOKEN}"
               for g in http.gets if "/user/info/" in g["url"])


def test_authenticate_via_token_provider_carries_only_read_scopes():
    # token_provider: the Vault-backed path. Same guarantee: the stored
    # read-only token comes back with its scopes untouched.
    http = _ReadOnlyTokenHttp()
    connector = tt.TikTokConnector(http=http, jitter_seconds=(0.0, 0.0))
    tokens = connector.authenticate(_account(),
                                    token_provider=lambda acc: _read_only_tokens())
    assert tokens.scopes == ["user.info.basic", "video.list"]
    assert UPLOAD_SCOPE not in tokens.scopes


def test_authenticate_without_token_source_still_raises():
    # Guard for the behaviour the old test relied on NOT existing:
    # with no auth_code, no TokenSet and no token_provider, authenticate
    # must fail closed — never fabricate a token to satisfy a scope test.
    connector = tt.TikTokConnector(http=_ReadOnlyTokenHttp(), jitter_seconds=(0.0, 0.0))
    with pytest.raises(tt.TikTokAuthError):
        connector.authenticate(_account())


def test_connector_still_reads_videos_and_stats():
    # Removing the write scope must not break the read paths it exists for.
    class FakeHttp:
        def get(self, url, headers, params):
            if url.endswith("/video/list/"):
                return 200, {"data": {"videos": [{"id": "tt1", "title": "T", "create_time": 1767225600,
                                                  "duration": 35}], "has_more": False}}
            if url.endswith("/video/query/"):
                return 200, {"data": {"videos": [{"id": "tt1", "statistics": {
                    "play_count": 50000, "digg_count": 4200, "comment_count": 310,
                    "share_count": 190, "collect_count": 260}}]}}
            return 200, {"data": {}}

    account = PlatformAccount(id="a1", workspace_id="w1", platform=Platform.TIKTOK,
                              platform_user_id="u1", credential_ref="vault:tiktok/a1")
    connector = tt.TikTokConnector(http=FakeHttp(), jitter_seconds=(0.0, 0.0))
    videos = connector.list_videos(account, limit=5)
    assert len(videos) == 1 and videos[0].platform_video_id == "tt1"
    metric = connector.get_video_stats(account, videos[0])
    assert metric.views == 50000 and metric.likes == 4200 and metric.saves == 260


if __name__ == "__main__":
    test_required_scopes_are_read_only_and_exact()
    test_no_write_scope_in_any_scope_list()
    test_docstring_states_read_only_contract()
    test_authenticate_token_carries_only_read_scopes()
    test_authenticate_via_token_provider_carries_only_read_scopes()
    test_authenticate_without_token_source_still_raises()
    test_connector_still_reads_videos_and_stats()
    print("ALL TESTS PASSED")
