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

Run from repo root:  python -m pytest tests/test_fix_tiktok_upload_scope.py -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.connectors.tiktok_connector as tt
from src.contracts import Platform, PlatformAccount

UPLOAD_SCOPE = "video." + "upload"
PUBLISH_SCOPE = "video." + "publish"
WRITE_SCOPES = {UPLOAD_SCOPE, PUBLISH_SCOPE}


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


class _FakeHttp:
    def get(self, url, headers, params):
        return 200, {"data": {}}


def test_authenticate_token_carries_only_read_scopes():
    account = PlatformAccount(id="a1", workspace_id="w1", platform=Platform.TIKTOK,
                              platform_user_id="u1", credential_ref="vault:tiktok/a1")
    connector = tt.TikTokConnector(http=_FakeHttp(), jitter_seconds=(0.0, 0.0))
    tokens = connector.authenticate(account)
    assert tokens.scopes == ["user.info.basic", "video.list"]
    assert UPLOAD_SCOPE not in tokens.scopes


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
    test_connector_still_reads_videos_and_stats()
    print("ALL TESTS PASSED")
