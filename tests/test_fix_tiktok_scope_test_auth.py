"""fix_tiktok_scope_test_auth — scope test theo authenticate() fail-closed.

Loi cu (da tai hien tren VM truoc khi fix): test scope goi
``connector.authenticate(account)`` khong auth_code, khong TokenSet,
khong token_provider, va ky vong nhan ve TokenSet de assert scope.
Tu ``fix_tiktok_auth_fabricated``, cuoc goi do PHAI raise
``TikTokAuthError`` ('no_token_source') — authenticate() khong bao
gio bia token. Loi o TEST, khong o connector; noi long authenticate()
de test cu xanh se tai sinh lo hong token gia vua va.

File nay kiem chung hanh vi dung: authenticate() chi tra token THAT
lay tu ``tokens=`` hoac ``token_provider`` (Vault) sau khi validate
live bang /user/info/ CO kem Bearer, token tra ve giu nguyen scopes
chi-doc, va khong co nguon token thi raise. Khong sua connector o
day.

GHI CHU — hardening chua co trong nhanh nay: theo phuong an hoi
dong, buoc tiep theo (commit rieng) la connector them kiem tra
whitelist scope trong authenticate()/_validate_token_live — TokenSet
tu Vault chua scope ngoai tap read-only cho phep thi raise
TikTokAuthError. Buoc do doi sua src/connectors/tiktok_connector.py
nen KHONG duoc nop trong nhanh fix/* nay (quy tac nhanh chi cho
nop 2 file test); test cho whitelist do cung chua co o day vi chung
se FAIL neu chua co code connector tuong ung. Khi nao co nhanh
rieng cho connector thi them test: token tu provider mang
video.upload -> authenticate() raise TikTokAuthError.

Stdlib + pytest only. Chay tu repo root:
pytest -q tests/test_fix_tiktok_scope_test_auth.py tests/test_fix_tiktok_upload_scope.py
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.connectors.tiktok_connector as tt
from src.contracts import Platform, PlatformAccount, TokenSet, utcnow

READ_SCOPES = ["user.info.basic", "video.list"]
_REAL_TOKEN = "tok-read-only"
_REFRESHED_TOKEN = "tok-refreshed"


def _account():
    return PlatformAccount(id="a1", workspace_id="w1", platform=Platform.TIKTOK,
                           platform_user_id="u1", credential_ref="vault:tiktok/a1")


def _tokens(*, access_token=_REAL_TOKEN, scopes=None, expired=False):
    at = utcnow() - timedelta(hours=1) if expired else utcnow() + timedelta(hours=1)
    return TokenSet(access_token=access_token, refresh_token="rt-1",
                    expires_at=at, scopes=list(scopes if scopes is not None else READ_SCOPES))


class _BearerOnlyHttp:
    """Chi chap nhan Bearer token that tren /user/info/; con lai 401."""

    def __init__(self, accepted: str):
        self.accepted = accepted
        self.gets: list[dict] = []

    def get(self, url, headers, params):
        self.gets.append({"url": url, "headers": dict(headers)})
        if "/user/info/" in url:
            if headers.get("Authorization") != f"Bearer {self.accepted}":
                return 401, {"error": {"code": "access_token_invalid",
                                       "message": "no/invalid token"}}
            return 200, {"data": {"open_id": "u1", "display_name": "Test"}}
        return 200, {"data": {}}


class _RefreshHttp(_BearerOnlyHttp):
    """Them POST /oauth/token/ tra token that moi (refresh flow)."""

    def __init__(self):
        super().__init__(_REFRESHED_TOKEN)
        self.posts: list[dict] = []

    def post(self, url, headers, data):
        self.posts.append({"url": url, "data": dict(data)})
        if "/oauth/token/" in url:
            return 200, {"access_token": _REFRESHED_TOKEN, "refresh_token": "rt-new",
                         "expires_in": 7200, "scope": "user.info.basic video.list",
                         "token_type": "Bearer"}
        return 404, {}


def _conn(http, **kw):
    return tt.TikTokConnector(http=http, jitter_seconds=(0.0, 0.0), **kw)


# --- authenticate() voi token that: tra ve nguyen trang, scopes giu nguyen ---

def test_authenticate_with_tokens_returns_same_read_only_token():
    http = _BearerOnlyHttp(_REAL_TOKEN)
    got = _conn(http).authenticate(_account(), tokens=_tokens())
    assert got.access_token == _REAL_TOKEN
    assert got.scopes == READ_SCOPES
    assert "video.upload" not in got.scopes and "video.publish" not in got.scopes
    # Validate live that su dung Bearer cua chinh token do.
    assert any(g["headers"].get("Authorization") == f"Bearer {_REAL_TOKEN}"
               for g in http.gets if "/user/info/" in g["url"])


def test_authenticate_with_token_provider_returns_read_only_token():
    http = _BearerOnlyHttp(_REAL_TOKEN)
    got = _conn(http).authenticate(_account(),
                                   token_provider=lambda acc: _tokens())
    assert got.access_token == _REAL_TOKEN
    assert got.scopes == READ_SCOPES


def test_authenticate_does_not_rewrite_scopes():
    # Connector khong duoc tu them/bot scope: token tra ve mang dung
    # scopes cua TokenSet da cung cap (ke ca scope phu chi-doc).
    scopes = ["user.info.basic", "video.list", "user.info.stats"]
    http = _BearerOnlyHttp(_REAL_TOKEN)
    got = _conn(http).authenticate(_account(), tokens=_tokens(scopes=scopes))
    assert got.scopes == scopes


def test_authenticate_live_validation_failure_raises():
    # /user/info/ 401 -> token chua duoc chung minh, khong tra ve.
    http = _BearerOnlyHttp("some-other-token")
    with pytest.raises(tt.TikTokAuthError):
        _conn(http).authenticate(_account(), tokens=_tokens())


def test_expired_stored_token_refreshes_for_real_then_returns_read_scopes():
    # Provider tra token het han co refresh_token: connector phai POST
    # /oauth/token/ that (grant_type=refresh_token), khong bia expiry.
    http = _RefreshHttp()
    conn = _conn(http, client_key="ck-test", client_secret="cs-test")
    got = conn.authenticate(_account(),
                            token_provider=lambda acc: _tokens(expired=True))
    assert got.access_token == _REFRESHED_TOKEN
    assert got.scopes == READ_SCOPES
    assert any(p["data"].get("grant_type") == "refresh_token" for p in http.posts)
    # Expiry lay tu expires_in that (7200s), khong phai +24h tu bia.
    assert got.expires_at is not None and got.expires_at > utcnow()


# --- fail-closed: khong nguon token thi raise, khong bia token --------------

def test_authenticate_without_token_source_raises_no_token_source():
    with pytest.raises(tt.TikTokAuthError) as exc:
        _conn(_BearerOnlyHttp(_REAL_TOKEN)).authenticate(_account())
    assert getattr(exc.value, "code", None) == "no_token_source"


def test_authenticate_with_provider_returning_none_raises():
    with pytest.raises(tt.TikTokAuthError):
        _conn(_BearerOnlyHttp(_REAL_TOKEN)).authenticate(
            _account(), token_provider=lambda acc: None)


def test_authenticate_with_expired_token_and_no_refresh_token_raises():
    stored = TokenSet(access_token=_REAL_TOKEN, refresh_token="",
                      expires_at=utcnow() - timedelta(hours=1),
                      scopes=list(READ_SCOPES))
    with pytest.raises(tt.TikTokAuthError):
        _conn(_BearerOnlyHttp(_REAL_TOKEN)).authenticate(
            _account(), token_provider=lambda acc: stored)


def test_authenticate_with_empty_token_raises():
    empty = TokenSet(access_token="", refresh_token="",
                     expires_at=utcnow() + timedelta(hours=1),
                     scopes=list(READ_SCOPES))
    with pytest.raises(tt.TikTokAuthError):
        _conn(_BearerOnlyHttp(_REAL_TOKEN)).authenticate(_account(), tokens=empty)
