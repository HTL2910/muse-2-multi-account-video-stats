# Sửa lỗi `fix/fix_api_cursor_offset`: Pagination chuyển sang keyset cursor có ký, chặn offset âm

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_api_cursor_offset_unstable (backend-sec). encode/decode_cursor hiện chỉ là offset base64: chèn bản ghi giữa các trang gây trùng/sót, offset âm cắt list Python từ cuối mà không báo lỗi. Fix: keyset cursor thật mã hóa (created_at, id) của bản ghi cuối + chữ ký HMAC ngắn chống giả mạo, truy vấn WHERE (created_at, id) > cursor ORDER BY created_at, id. Validate cursor không hợp lệ, âm hoặc vượt giới hạn thì 400. Test phân trang khi chèn bản ghi giữa hai trang không trùng/sót.

## Bài làm

Đã kiểm thử trên máy ảo (venv riêng cài fastapi/httpx, stub contracts v1.0.0): cả 7 test PASS, gồm case trung tâm — lấy trang 1 (acc1, acc2), chèn bản ghi có khóa nằm giữa hai trang (accX) và một bản ghi trước trang 1, trang 2 trả đúng [accX, acc3], trang 3 [acc4, acc5], không trùng/không sót; cursor bị sửa chữ ký, cursor offset cũ `{"o": -1}` và cursor rác đều bị 400; smoke test thêm cho /videos, /proxies, /videos/{id}/metrics, /jobs đều phân trang keyset đúng. Thiết kế: cursor dạng `<payload_b64url>.<HMAC-SHA256 cắt 16 byte>` chứa `{"v":2,"k":[created_at ISO-8601 UTC, id]}`, secret từ env `CURSOR_SIGNING_SECRET`/`JWT_SECRET`; `paginate_keyset()` tương đương `WHERE (created_at, id) > cursor ORDER BY created_at, id`; các endpoint dùng khóa riêng (accounts/videos: `(created_at, id)`, metrics: `(collected_at, idempotency_key)`, jobs: `(created_at, id)` — job dict nay ghi thêm `created_at` khi enqueue), limit được chặn `ge=1, le=200`. Chỉ nộp file mục tiêu + file test riêng.

(đã nộp file `src/api/main.py`)

(đã nộp file `tests/test_fix_api_cursor_offset.py`)

## File đã nộp

- `src/api/main.py`
- `tests/test_fix_api_cursor_offset.py`
