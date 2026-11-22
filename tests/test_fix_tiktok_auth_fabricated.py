"""fix_tiktok_auth_fabricated — authenticate/refresh tra token THAT hoac raise.

Loi cu (da tai hien tren VM truoc khi fix): ``authenticate()`` goi
``/user/info/`` khong kem token, bat loi roi bo qua va tra TokenSet
khong co access_token; ``refresh_token()`` tra TokenSet voi expires_at
+24h tu bia ma khong he POST ``/oauth/token/``.

Test contract chung cho ca 3 connector (ap dung o day cho TikTok):
token tra ve phai co access_token non-empty va chua het han.

FakeHttp o day dong vai TikTok that: POST /oauth/token/ tra token
that, GET /user/info/ chi chap nhan Bearer token do, 401 neu sai.

Stdlib + pytest only. Chay tu repo root:
pytest -q tests/test_fix_tiktok_auth_fabricated.py
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contracts import (  # noqa: E402
    AccountStatus,
    Platform,
    PlatformAccount,
    TokenSet,
    utcnow,
)
from src.connectors.tiktok_connector import (  # noqa: E402
    TikTokAuthError,
    TikTokConnector,
)


def _acc() -> PlatformAccount:
    return PlatformAccount(
        id="acc-1", workspace_id="ws-1", platform=Platform.TIKTOK,
        platform_user_id="u1", status=AccountStatus.ACTIVE,
        credential_ref="vault://tok-1",
    )


def _real_token(**kw) -> TokenSet:
    base = dict(access_token="tok-real", refresh_token="rt-real",
                expires_at=utcnow() + timedelta(hours=1),
                scopes=["user.info.basic", "video.list"])
    base.update(kw)
    return TokenSet(**base)


def _assert_real_token(tokens: TokenSet) -> None:
    """Contract chung: token tra ve phai co access_token va chua het han."""
    assert tokens.access_token, "token tra ve khong co access_token"
    assert isinstance(tokens.access_token, str) and tokens.access_token.strip()
    assert tokens.expires_at is not None, "token tra ve khong co expires_at that"
    assert not tokens.is_expired(), "token tra ve da het han"
    assert tokens.expires_at > utcnow()


class FakeTikTok:
    """Gia lap TikTok: POST /oauth/token/ + GET /user/info/ co kem Bearer."""

    def __init__(self, *, token_response=None, user_info_status=200):
        self.token_response = token_response or {
            "access_token": "tok-new", "refresh_token": "rt-new",
            "expires_in": 86400, "scope": "user.info.basic video.list",
            "token_type": "Bearer",
        }
        self.user_info_status = user_info_status
        self.posts: list[dict] = []
        self.gets: list[dict] = []

    def post(self, url, headers, data):
        self.posts.append({"url": url, "data": dict(data)})
        if "/oauth/token/" in url:
            return 200, dict(self.token_response)
        return 404, {}

    def get(self, url, headers, params):
        self.gets.append({"url": url, "headers": dict(headers), "params": dict(params or {})})
        if "/user/info/" in url:
            if self.user_info_status != 200:
                return self.user_info_status, {"error": {"code": "access_token_invalid", "message": "bad token"}}
            accepted = {"Bearer tok-new", "Bearer tok-real",
                        f"Bearer {self.token_response.get('access_token')}"}
            if headers.get("Authorization") not in accepted:
                return 401, {"error": {"code": "access_token_invalid", "message": "no/invalid token"}}
            return 200, {"data": {"open_id": "u1", "display_name": "Test"}}
        return 200, {"data": {}}


def _conn(http, **kw) -> TikTokConnector:
    return TikTokConnector(http=http, jitter_seconds=(0, 0),
                           client_key="ck-test", client_secret="cs-test", **kw)


# --- authenticate: doi auth code that ---------------------------------------

def test_authenticate_with_auth_code_returns_real_token():
    http = FakeTikTok()
    conn = _conn(http)
    tokens = conn.authenticate(_acc(), auth_code="code-real")
    _assert_real_token(tokens)
    assert tokens.access_token == "tok-new"
    assert tokens.refresh_token == "rt-new"
    # POST that dung grant_type authorization_code, khong phai gia lap.
    assert len(http.posts) == 1
    assert http.posts[0]["url"].endswith("/oauth/token/")
    assert http.posts[0]["data"]["grant_type"] == "authorization_code"
    assert http.posts[0]["data"]["code"] == "code-real"
    # /user/info/ phai kem Bearer token moi (loi cu: goi khong kem token).
    assert http.gets and http.gets[0]["headers"].get("Authorization") == "Bearer tok-new"
    # expires_at lay tu expires_in that (86400s), khong phai +24h tu bia.
    assert tokens.expires_at > utcnow() + timedelta(hours=23)


def test_authenticate_with_auth_code_missing_client_credentials_raises():
    http = FakeTikTok()
    conn = TikTokConnector(http=http, jitter_seconds=(0, 0))  # khong co client_key/secret
    with pytest.raises(TikTokAuthError):
        conn.authenticate(_acc(), auth_code="code-real")
    assert http.posts == []  # khong duoc goi gi khi thieu credentials


def test_authenticate_with_auth_code_user_info_rejects_token_raises():
    http = FakeTikTok(user_info_status=401)
    conn = _conn(http)
    with pytest.raises(TikTokAuthError):
        conn.authenticate(_acc(), auth_code="code-real")


# --- authenticate: TokenSet / Vault provider ---------------------------------

def test_authenticate_with_stored_token_set_validates_live():
    http = FakeTikTok()
    conn = _conn(http)
    tokens = conn.authenticate(_acc(), tokens=_real_token())
    _assert_real_token(tokens)
    assert tokens.access_token == "tok-real"
    assert http.posts == []  # token da co that, khong can doi code
    assert http.gets[0]["headers"].get("Authorization") == "Bearer tok-real"


def test_authenticate_with_empty_token_set_raises():
    conn = _conn(FakeTikTok())
    with pytest.raises(TikTokAuthError):
        conn.authenticate(_acc(), tokens=TokenSet())
    with pytest.raises(TikTokAuthError):
        conn.authenticate(_acc(), tokens=_real_token(expires_at=utcnow() - timedelta(seconds=1)))


def test_authenticate_with_token_provider_vault_returns_real_token():
    http = FakeTikTok()
    conn = _conn(http, token_provider=lambda acc: _real_token())
    tokens = conn.authenticate(_acc())
    _assert_real_token(tokens)
    assert tokens.access_token == "tok-real"
    assert http.gets[0]["headers"].get("Authorization") == "Bearer tok-real"


def test_authenticate_with_no_token_source_raises_instead_of_fabricating():
    # Loi cu: truong hop nay tra TokenSet rong. Ban moi: raise.
    conn = _conn(FakeTikTok())
    with pytest.raises(TikTokAuthError):
        conn.authenticate(_acc())
    with pytest.raises(TikTokAuthError):
        conn.authenticate(_acc(), token_provider=lambda acc: None)


def test_authenticate_expired_stored_token_refreshes_for_real():
    http = FakeTikTok()
    conn = _conn(http, token_provider=lambda acc: _real_token(
        expires_at=utcnow() - timedelta(seconds=1)))
    tokens = conn.authenticate(_acc())
    _assert_real_token(tokens)
    assert tokens.access_token == "tok-new"  # token moi tu POST refresh
    assert http.posts and http.posts[0]["data"]["grant_type"] == "refresh_token"


# --- refresh_token: POST that -------------------------------------------------

def test_refresh_token_posts_oauth_token_and_returns_real_token():
    http = FakeTikTok(token_response={
        "access_token": "tok-refreshed", "refresh_token": "rt-refreshed",
        "expires_in": 7200, "scope": "user.info.basic video.list",
    })
    conn = _conn(http)
    tokens = conn.refresh_token(_acc(), _real_token())
    _assert_real_token(tokens)
    assert tokens.access_token == "tok-refreshed"
    assert tokens.refresh_token == "rt-refreshed"
    assert len(http.posts) == 1
    assert http.posts[0]["data"]["grant_type"] == "refresh_token"
    assert http.posts[0]["data"]["refresh_token"] == "rt-real"
    # expires_at = now + 7200s that, khong phai +24h tu bia.
    assert utcnow() + timedelta(hours=1) < tokens.expires_at < utcnow() + timedelta(hours=3)


def test_refresh_token_missing_refresh_token_raises():
    conn = _conn(FakeTikTok())
    with pytest.raises(TikTokAuthError):
        conn.refresh_token(_acc(), TokenSet(access_token="tok-real"))


def test_refresh_token_missing_client_credentials_raises():
    http = FakeTikTok()
    conn = TikTokConnector(http=http, jitter_seconds=(0, 0))
    with pytest.raises(TikTokAuthError):
        conn.refresh_token(_acc(), _real_token())
    assert http.posts == []


def test_refresh_token_response_without_access_token_raises():
    http = FakeTikTok(token_response={"expires_in": 86400, "refresh_token": "rt-new"})
    conn = _conn(http)
    with pytest.raises(TikTokAuthError):
        conn.refresh_token(_acc(), _real_token())


def test_refresh_token_response_without_expires_in_raises_never_fabricates_expiry():
    # Loi cu: truong hop nay van tra expires_at +24h tu bia.
    http = FakeTikTok(token_response={"access_token": "tok-new", "refresh_token": "rt-new"})
    conn = _conn(http)
    with pytest.raises(TikTokAuthError):
        conn.refresh_token(_acc(), _real_token())


def test_refresh_token_invalid_grant_raises_auth_error():
    class BadGrant(FakeTikTok):
        def post(self, url, headers, data):
            self.posts.append({"url": url, "data": dict(data)})
            return 401, {"error": {"code": "invalid_grant", "message": "refresh token revoked"}}

    conn = _conn(BadGrant())
    with pytest.raises(TikTokAuthError):
        conn.refresh_token(_acc(), _real_token())


# --- Tuong thich nguoc: list_videos van chay voi client chi co get ----------

class _GetOnlyHttp:
    def get(self, url, headers, params):
        return 200, {"data": {"videos": [
            {"id": "v1", "title": "t1", "cover_image_url": "", "create_time": 1700000000,
             "duration": 10, "share_url": "https://example.invalid/v1"},
        ], "cursor": None, "has_more": False}}


def test_list_videos_still_works_with_get_only_client():
    conn = TikTokConnector(http=_GetOnlyHttp(), jitter_seconds=(0, 0))
    videos = conn.list_videos(_acc(), limit=10)
    assert len(videos) == 1 and videos[0].platform_video_id == "v1"
