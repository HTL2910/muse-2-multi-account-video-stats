# Sửa lỗi `fix/fix_tiktok_auth_fabricated`: TikTok authenticate/refresh phải trả token thật hoặc raise AuthError

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp fix_tiktok_authenticate_refresh_fabricat (algo-ml). authenticate() gọi /user/info/ không kèm token, bắt lỗi rồi bỏ qua và trả TokenSet không có access_token; refresh_token() trả TokenSet với expires_at +24h tự bịa mà không POST /oauth/token/. Fix: authenticate() phải thực hiện OAuth exchange thật qua Vault hoặc raise TikTokAuthError nếu không có token; refresh_token() phải POST /oauth/token/ và trả access_token/refresh_token thật; không bao giờ trả TokenSet với expires_at tự bịa. Tái hiện trước khi fix theo phản biện systems-arch. Test contract chung cho cả 3 connector: token trả về phải có access_token non-empty và chưa hết hạn.

## Bài làm

Đã tái hiện lỗi trước khi fix trên máy ảo Linux: `authenticate()` gọi `/user/info/` không kèm token và trả `TokenSet` không có `access_token`; `refresh_token()` trả `TokenSet` với `expires_at` +24h tự bịa mà không POST `/oauth/token/`.

Đã chạy kiểm thử trên máy ảo Linux (venv pytest 9.2.0, Python 3.12.3): **32 passed** — 15 test mới `test_fix_tiktok_auth_fabricated.py` + 17 test cũ `test_algo_ml_m2.py`, không regression.

Thay đổi chính trong `src/connectors/tiktok_connector.py` (dựa trên `main` như đã đọc, giữ nguyên phần read-only scope và các hàm map/list/stats):
- `authenticate()` chỉ trả token thật: đổi `auth_code` thật qua POST `/oauth/token/` (grant_type=authorization_code), hoặc dùng `TokenSet` thật caller đưa, hoặc `token_provider` Vault-backed (token hết hạn mà có refresh_token thì refresh thật trước); rồi xác thực live bằng `/user/info/` **có kèm** Bearer token. Không có nguồn token thật nào thì raise `TikTokAuthError`, không bao giờ trả TokenSet rỗng.
- `refresh_token()` bắt buộc POST `/oauth/token/` (grant_type=refresh_token) với `client_key`/`client_secret`, chỉ trả access_token/refresh_token/expires_at do TikTok trả về (`expires_in` thật, thiếu access_token hoặc expires_in hợp lệ thì raise, không bịa +24h); token mới cũng được xác thực live trước khi trả về. Thiếu refresh_token hay client credentials thì raise.
- Thêm `post()` cho `_HttpClient`/`_UrllibClient` (form-encoded), thêm field `client_key`/`client_secret`/`token_provider` (repr=False, không log), và guard `_require_real_token()` (access_token non-empty, chưa hết hạn).

(đã nộp file `src/connectors/tiktok_connector.py`)

(đã nộp file `tests/test_fix_tiktok_auth_fabricated.py`)

## File đã nộp

- `src/connectors/tiktok_connector.py`
- `tests/test_fix_tiktok_auth_fabricated.py`
