"""Regression tests for fix_api_cursor_offset.

Old behaviour: encode/decode_cursor was a base64 OFFSET ({"o": n}).
Inserting a record between pages shifted every later page (duplicates /
skips), and a negative offset sliced the Python list from the end with
no error. New behaviour: signed KEYSET cursor encoding (created_at, id)
of the last record of the previous page; the next page is
WHERE (created_at, id) > cursor ORDER BY created_at, id. Tampered,
legacy-offset, or malformed cursors are rejected with 400.

Run from repo root:  python -m pytest tests/test_fix_api_cursor_offset.py -q
"""
import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.main import (InMemoryStore, create_app, decode_cursor,
                          encode_cursor, paginate_keyset)
from src.contracts import Platform, PlatformAccount

UTC = timezone.utc
T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _legacy_offset_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def _expect_400(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except HTTPException as exc:
        assert exc.status_code == 400, f"expected 400, got {exc.status_code}"
        return
    raise AssertionError("expected HTTPException 400")


# ---------------------------------------------------------------- unit: cursor codec
def test_cursor_roundtrip_is_keyset_not_offset():
    cur = encode_cursor(T0 + timedelta(hours=2), "acc-42")
    assert "." in cur  # payload.signature, not a bare base64 offset blob
    body = cur.split(".")[0]
    payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode())
    assert payload["v"] == 2 and "o" not in payload
    assert payload["k"][1] == "acc-42"
    ts, iid = decode_cursor(cur)
    assert ts == T0 + timedelta(hours=2) and iid == "acc-42"
    assert decode_cursor(None) is None and decode_cursor("") is None


def test_decode_rejects_tampered_legacy_and_garbage_cursors():
    cur = encode_cursor(T0, "acc-1")
    body, sig = cur.split(".")
    tampered = body + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
    _expect_400(decode_cursor, tampered)                       # forged signature
    _expect_400(decode_cursor, cur, secret="another-secret")   # wrong secret
    _expect_400(decode_cursor, _legacy_offset_cursor(5))       # legacy offset format
    _expect_400(decode_cursor, _legacy_offset_cursor(-1))      # negative offset (was: silent tail slice)
    _expect_400(decode_cursor, "not-a-cursor")
    _expect_400(decode_cursor, base64.urlsafe_b64encode(b"{}").decode())


# ------------------------------------------------- unit: keyset pagination semantics
def test_paginate_keyset_insert_between_pages_no_duplicates_no_skips():
    def rec(i, name):
        return {"id": name, "created_at": T0 + timedelta(hours=i)}
    items = [rec(i, f"a{i}") for i in range(1, 6)]  # a1..a5, hourly
    key = lambda r: (r["created_at"], r["id"])

    page1, cur1, total = paginate_keyset(items, cursor=None, limit=2, key=key)
    assert [r["id"] for r in page1] == ["a1", "a2"] and total == 5 and cur1

    # Insert BETWEEN pages: one record after the cursor (a2 < aX < a3) and
    # one before the first page (a0 < a1). Offset pagination would shift
    # page 2 and duplicate a2 / skip a3; keyset must not.
    items = items + [{"id": "aX", "created_at": T0 + timedelta(hours=2, minutes=30)},
                     {"id": "a0", "created_at": T0 - timedelta(hours=1)}]
    page2, cur2, _ = paginate_keyset(items, cursor=cur1, limit=2, key=key)
    assert [r["id"] for r in page2] == ["aX", "a3"]
    page3, cur3, _ = paginate_keyset(items, cursor=cur2, limit=2, key=key)
    assert [r["id"] for r in page3] == ["a4", "a5"] and cur3 is None

    seen = [r["id"] for r in page1 + page2 + page3]
    assert len(seen) == len(set(seen)), f"duplicates across pages: {seen}"
    assert set(seen) == {"a1", "a2", "aX", "a3", "a4", "a5"}  # nothing skipped; a0 is before the cursor, correctly not re-served


def test_paginate_keyset_ties_break_by_id():
    same_ts = [{"id": "b", "created_at": T0}, {"id": "a", "created_at": T0},
               {"id": "c", "created_at": T0}]
    key = lambda r: (r["created_at"], r["id"])
    p1, cur, _ = paginate_keyset(same_ts, cursor=None, limit=2, key=key)
    p2, cur2, _ = paginate_keyset(same_ts, cursor=cur, limit=2, key=key)
    assert [r["id"] for r in p1] == ["a", "b"] and [r["id"] for r in p2] == ["c"] and cur2 is None


# ---------------------------------------------------------------- API end-to-end
def _client_with_accounts(n=5):
    store = InMemoryStore()
    for i in range(1, n + 1):
        a = PlatformAccount(id=f"acc{i}", workspace_id="w1", platform=Platform.YOUTUBE,
                            platform_user_id=f"u{i}", display_name=f"Acc {i}",
                            credential_ref="vault:x", created_at=T0 + timedelta(hours=i))
        store.accounts[a.id] = a
    app = create_app(store=store, auth_checker=lambda _t: {"workspace_id": "w1"})
    return TestClient(app), store


def test_api_accounts_pagination_stable_when_inserting_between_pages():
    client, store = _client_with_accounts()
    r1 = client.get("/accounts", params={"workspace_id": "w1", "limit": 2})
    assert r1.status_code == 200
    d1 = r1.json()
    assert [x["id"] for x in d1["items"]] == ["acc1", "acc2"] and d1["total"] == 5

    mid = PlatformAccount(id="accX", workspace_id="w1", platform=Platform.YOUTUBE,
                          platform_user_id="uX", display_name="Inserted",
                          credential_ref="vault:x",
                          created_at=T0 + timedelta(hours=2, minutes=30))
    store.accounts[mid.id] = mid

    r2 = client.get("/accounts", params={"workspace_id": "w1", "limit": 2, "cursor": d1["next_cursor"]})
    assert r2.status_code == 200
    d2 = r2.json()
    assert [x["id"] for x in d2["items"]] == ["accX", "acc3"]
    r3 = client.get("/accounts", params={"workspace_id": "w1", "limit": 2, "cursor": d2["next_cursor"]})
    d3 = r3.json()
    assert [x["id"] for x in d3["items"]] == ["acc4", "acc5"] and d3["next_cursor"] is None
    seen = [x["id"] for x in d1["items"] + d2["items"] + d3["items"]]
    assert len(seen) == len(set(seen)) == 6


def test_api_rejects_invalid_cursors_with_400():
    client, _store = _client_with_accounts(2)
    for bad in (_legacy_offset_cursor(1), _legacy_offset_cursor(-1), "garbage!!"):
        r = client.get("/accounts", params={"workspace_id": "w1", "cursor": bad})
        assert r.status_code == 400, f"cursor {bad!r} -> {r.status_code}, expected 400"
    good = encode_cursor(T0 + timedelta(hours=1), "acc1")
    body, sig = good.split(".")
    forged = body + "." + ("A" if sig[0] != "A" else "B") + sig[1:]
    r = client.get("/accounts", params={"workspace_id": "w1", "cursor": forged})
    assert r.status_code == 400


def test_api_limit_bounds_validated():
    client, _store = _client_with_accounts(2)
    assert client.get("/accounts", params={"workspace_id": "w1", "limit": 0}).status_code == 422
    assert client.get("/accounts", params={"workspace_id": "w1", "limit": 201}).status_code == 422
    assert client.get("/videos", params={"limit": -3}).status_code == 422


if __name__ == "__main__":
    test_cursor_roundtrip_is_keyset_not_offset()
    test_decode_rejects_tampered_legacy_and_garbage_cursors()
    test_paginate_keyset_insert_between_pages_no_duplicates_no_skips()
    test_paginate_keyset_ties_break_by_id()
    test_api_accounts_pagination_stable_when_inserting_between_pages()
    test_api_rejects_invalid_cursors_with_400()
    test_api_limit_bounds_validated()
    print("ALL TESTS PASSED")
