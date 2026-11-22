# Sửa lỗi `fix/fix_facebook_token_thumbnail`: Facebook token lấy expires_in thật và parse thumbnail an toàn

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_facebook_token_expires_immediately + fix_facebook_thumbnail_empty_crash (systems-arch, cả hai đã kiểm chứng VM). authenticate() trả expires_at=utcnow() nên token vừa đổi đã hết hạn theo TokenSet.is_expired()/needs_refresh(); parse_facebook_video crash IndexError khi thumbnails.data=[] (mặc định [{}] chỉ cứu khi thiếu khóa). Fix: lấy expires_in từ response fb_exchange_token, expires_at=utcnow()+timedelta(seconds=expires_in), mặc định +60 ngày nếu API không trả và ghi rõ, không dùng utcnow(); lấy thumbnail an toàn: data=(thumbnails or {}).get('data') or []; thumbnail_url=data[0].get('uri','') if data else ''. Test: token vừa đổi chưa expired; fixture thumbnails data=[] không crash và không giết cả batch.

## Bài làm

Nhánh `fix/fix_facebook_token_thumbnail` — chỉ nộp 2 file theo quy tắc nhánh `fix/*`. Thay đổi duy nhất trong `facebook_connector.py`: (1) `authenticate()` lấy `expires_in` thật từ `fb_exchange_token`, `expires_at = utcnow() + timedelta(seconds=expires_in)`, thiếu/không hợp lệ thì mặc định +60 ngày và ghi rõ; (2) thêm helper `_thumbnail_url()` an toàn, `data=[]`/thiếu/sai kiểu trả `""` thay vì `IndexError`. Phần còn lại giữ nguyên, kể cả khối import try/except (thuộc phạm vi `fix_import_dual_contracts`, không sửa ở nhánh này).

Kiểm thử đã chạy trên VM Linux (Python 3.12.3, pytest 9.2.0) với `http_get` fake, không gọi Graph API thật: `pytest tests/test_fix_facebook_token_thumbnail.py` = **24 passed**. Vì `src/contracts.py`/`src/core/models.py` thật trên `main` không có trên VM này, bản kiểm thử dùng stub trung thực tại chỗ (chỉ để kiểm thử, không nộp) với đúng enum/dataclass/chữ ký từ contract đã khóa; kết quả thật trên `main` do CI báo cáo.

(đã nộp file `src/connectors/facebook_connector.py`)

(đã nộp file `tests/test_fix_facebook_token_thumbnail.py`)

## File đã nộp

- `src/connectors/facebook_connector.py`
- `tests/test_fix_facebook_token_thumbnail.py`
