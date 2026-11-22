# Sửa lỗi `fix/fix_facebook_token_thumbnail`: Facebook token lấy expires_in thật và parse thumbnail an toàn

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 2_

## Việc được giao

Gộp fix_facebook_token_expires_immediately + fix_facebook_thumbnail_empty_crash (systems-arch, cả hai đã kiểm chứng VM). authenticate() trả expires_at=utcnow() nên token vừa đổi đã hết hạn theo TokenSet.is_expired()/needs_refresh(); parse_facebook_video crash IndexError khi thumbnails.data=[] (mặc định [{}] chỉ cứu khi thiếu khóa). Fix: lấy expires_in từ response fb_exchange_token, expires_at=utcnow()+timedelta(seconds=expires_in), mặc định +60 ngày nếu API không trả và ghi rõ, không dùng utcnow(); lấy thumbnail an toàn: data=(thumbnails or {}).get('data') or []; thumbnail_url=data[0].get('uri','') if data else ''. Test: token vừa đổi chưa expired; fixture thumbnails data=[] không crash và không giết cả batch.

## Bài làm

Nhánh `fix/fix_facebook_token_thumbnail` — sửa lại sau lần CI fail, chỉ nộp 2 file theo quy tắc nhánh `fix/*`.

Nguyên nhân 10 failed: **lỗi ở fixture test, không phải ở code fix**. `PlatformAdapterBase.guard()` thật trên `main` (`src/core/models.py:208`) fail-closed và raise `AdapterAuthError` khi `account.credential_ref` trống; fixture `_account()` của tôi bỏ trống trường này nên `authenticate()`/`refresh_token()` bị guard chặn đúng luật. 14 test thumbnail vẫn pass vì `parse_facebook_video()` không gọi guard. Stub guard trên VM của tôi trước đây thiếu kiểm tra này nên không bắt được lỗi — đã sửa stub cho khớp guard thật rồi chạy lại.

Sửa test: `_account()` thêm `credential_ref="vault://facebook/acc-1"` (chỉ là con trỏ tới Vault, không phải secret), và thêm test hồi quy `test_authenticate_still_fail_closed_without_credential_ref` chứng minh account không có `credential_ref` vẫn bị chặn. Code `facebook_connector.py` không đổi so với bản đã nộp trước (vẫn `expires_in` thật + `_thumbnail_url()` an toàn).

Kiểm thử trên VM Linux (Python 3.12.3, pytest 9.2.0), với guard stub đã khớp bản thật: `pytest tests/test_fix_facebook_token_thumbnail.py` = **25 passed** (24 cũ + 1 hồi quy mới).

(đã nộp file `src/connectors/facebook_connector.py`)

(đã nộp file `tests/test_fix_facebook_token_thumbnail.py`)

## File đã nộp

- `src/connectors/facebook_connector.py`
- `tests/test_fix_facebook_token_thumbnail.py`
