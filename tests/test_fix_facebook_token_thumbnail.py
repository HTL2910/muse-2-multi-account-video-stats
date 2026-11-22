"""fix_facebook_token_thumbnail — token lay expires_in that, thumbnail an toan.

- authenticate() cu: expires_at=utcnow() nen token vua doi da het han
  theo TokenSet.is_expired()/needs_refresh().
- parse_facebook_video cu: thumbnails.data=[] crash IndexError, mac dinh
  [{}] chi cuu khi thieu khoa.

Test dung http_get fake (khong goi Graph API that, khong credential that).
Tu dua repo root len sys.path de khong phu thuoc conftest/pyproject.
"""
from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.connectors.facebook_connector import (
    DEFAULT_TOKEN_TTL_SECONDS,
    FacebookConnector,
    parse_facebook_video,
)
from src.contracts import AccountStatus, Platform, PlatformAccount, TokenSet, utcnow


def _account() -> PlatformAccount:
    # guard() that trong PlatformAdapterBase (src/core/models.py) fail-closed:
    # account khong co credential_ref -> AdapterAuthError. Day la con tro
    # toi Vault, khong phai secret. Bo qua truong nay la loi cua fixture
    # test (10 failed o CI truoc), khong phai loi cua authenticate().
    return PlatformAccount(
        id="acc-1", workspace_id="ws-1", platform=Platform.FACEBOOK,
        platform_user_id="page-1", status=AccountStatus.ACTIVE,
        credential_ref="vault://facebook/acc-1",
    )


def _connector(http_get):
    return FacebookConnector(
        token_resolver=lambda a: TokenSet(access_token="short-token", refresh_token="rt"),
        http_get=http_get, app_id="app-1", app_secret="secret-from-vault",
    )


def _fake_exchange(expires_in):
    def http_get(url, headers):
        assert "oauth/access_token" in url
        assert "fb_exchange_token" in url
        body = {"access_token": "long-token", "token_type": "bearer"}
        if expires_in is not None:
            body["expires_in"] = expires_in
        return body
    return http_get


@pytest.mark.parametrize("expires_in,expected_seconds", [
    (3600, 3600),
    ("5184000", 5184000),  # API co the tra chuoi
    (60 * 24 * 3600, 60 * 24 * 3600),
])
def test_authenticate_uses_real_expires_in(expires_in, expected_seconds):
    token = _connector(_fake_exchange(expires_in)).authenticate(_account())
    assert token.access_token == "long-token"
    assert token.expires_at is not None
    delta = (token.expires_at - utcnow()).total_seconds()
    assert abs(delta - expected_seconds) < 5, delta
    assert not token.is_expired()
    # 3600s > nguong refresh 30 phut nen chua can refresh; 60 ngay cung vay.
    assert not token.needs_refresh()


@pytest.mark.parametrize("expires_in", [None, 0, -10, "not-a-number", ""])
def test_authenticate_defaults_to_60_days_when_expires_in_missing_or_invalid(expires_in):
    token = _connector(_fake_exchange(expires_in)).authenticate(_account())
    delta = (token.expires_at - utcnow()).total_seconds()
    assert abs(delta - DEFAULT_TOKEN_TTL_SECONDS) < 5, delta
    assert DEFAULT_TOKEN_TTL_SECONDS == 60 * 24 * 3600
    assert not token.is_expired()


def test_authenticate_never_returns_expires_at_now():
    token = _connector(_fake_exchange(3600)).authenticate(_account())
    assert token.expires_at > utcnow()


def test_source_no_longer_sets_expires_at_to_utcnow():
    import re

    src = (REPO_ROOT / "src" / "connectors" / "facebook_connector.py").read_text(encoding="utf-8")
    assert "expires_in" in src
    # expires_at phai la utcnow() + timedelta(...), khong phai utcnow() tran.
    assert not re.search(r"expires_at=utcnow\(\)\s*[\)\n]", src)


@pytest.mark.parametrize("raw_thumbs,expected", [
    ({"data": []}, ""),
    ({}, ""),
    ({"data": [{"uri": "https://cdn.example/t.jpg"}]}, "https://cdn.example/t.jpg"),
    ({"data": [{}]}, ""),
    ({"data": [{"uri": None}]}, ""),
    ({"data": [None]}, ""),
    ({"data": "not-a-list"}, ""),
    ({"data": None}, ""),
])
def test_parse_thumbnail_safe(raw_thumbs, expected):
    video = parse_facebook_video(
        {"id": "v1", "title": "T", "thumbnails": raw_thumbs}, account=_account()
    )
    assert video.thumbnail_url == expected


@pytest.mark.parametrize("raw", [
    {"id": "v1", "title": "T"},  # khong co thumbnails
    {"id": "v1", "title": "T", "thumbnails": None},
    {"id": "v1", "title": "T", "thumbnails": []},
    {"id": "v1", "title": "T", "thumbnails": "https://cdn.example/t.jpg"},
])
def test_parse_thumbnail_missing_or_wrong_type(raw):
    assert parse_facebook_video(raw, account=_account()).thumbnail_url == ""


def test_empty_thumbnail_does_not_kill_batch():
    raws = [
        {"id": "v1", "title": "A", "thumbnails": {"data": [{"uri": "https://cdn.example/a.jpg"}]}},
        {"id": "v2", "title": "B", "thumbnails": {"data": []}},
        {"id": "v3", "title": "C"},
        {"id": "v4", "title": "D", "thumbnails": {"data": [{"uri": "https://cdn.example/d.jpg"}]}},
    ]
    videos = [parse_facebook_video(r, account=_account()) for r in raws]
    assert [v.platform_video_id for v in videos] == ["v1", "v2", "v3", "v4"]
    assert videos[0].thumbnail_url == "https://cdn.example/a.jpg"
    assert videos[1].thumbnail_url == ""
    assert videos[2].thumbnail_url == ""
    assert videos[3].thumbnail_url == "https://cdn.example/d.jpg"


def test_authenticate_still_fail_closed_without_credential_ref():
    """Hoi quy: sua fixture khong duoc lam mat guard — account khong co
    credential_ref van phai bi AdapterAuthError, khong phai token gia."""
    from src.core.models import AdapterAuthError

    bare = PlatformAccount(
        id="acc-1", workspace_id="ws-1", platform=Platform.FACEBOOK,
        platform_user_id="page-1", status=AccountStatus.ACTIVE,
    )
    assert not bare.credential_ref
    with pytest.raises(AdapterAuthError):
        _connector(_fake_exchange(3600)).authenticate(bare)


def test_refresh_token_uses_new_authenticate_when_needs_refresh():
    connector = _connector(_fake_exchange(3600))
    old = TokenSet(access_token="short-token", refresh_token="rt",
                   expires_at=utcnow() + timedelta(minutes=10))
    assert old.needs_refresh()
    new = connector.refresh_token(_account(), old)
    assert new.access_token == "long-token"
    assert not new.is_expired()
