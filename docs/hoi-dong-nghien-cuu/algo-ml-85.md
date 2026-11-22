# Sửa lỗi `fix/fix_tiktok_scope_test_auth`: Sửa test scope TikTok theo authenticate() fail-closed và ép whitelist scope

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp 5 báo cáo (muse fix_tiktok_scope_test_no_token_source, systems-arch fix_tiktok_authenticate_test_no_token_so, algo-ml fix_tiktok_scope_test_auth_conflict, backend-sec fix_tiktok_scope_guarantee_conflict, fullstack-ux fix_tiktok_scope_test_vs_auth_fabricated). Lỗi ở TEST: test_authenticate_token_carries_only_read_scopes gọi authenticate() không auth_code/tokens/token_provider nên connector raise TikTokAuthError 'no_token_source' đúng luật fix_tiktok_auth_fabricated; tuyệt đối không nới lỏng authenticate() để test xanh (đó là tái sinh lỗi bịa token). Sửa test: truyền tokens=TokenSet(access_token thật, scopes=['user.info.basic', 'video.list']) hoặc token_provider trả TokenSet đó, kèm FakeHttp mà GET /user/info/ chỉ chấp nhận Bearer token đó (theo mẫu FakeTikTok trong tests/test_fix_tiktok_auth_fabricated.py), rồi assert token trả về giữ đúng read scopes và không chứa video.upload/video.publish; giữ nguyên các test scope còn lại đang xanh. Commit riêng thứ hai trong cùng nhánh (hardening defense-in-depth, không trộn với sửa test): connector thêm kiểm tra whitelist scope trong authenticate()/_validate_token_live — TokenSet từ Vault chứa scope ngoài tập read-only cho phép thì raise TikTokAuthError, biến cam kết chỉ-đọc thành cơ chế thực thi trên cả token đã lưu.

## Bài làm

Nhánh `fix/fix_tiktok_scope_test_auth` — algo-ml đã tái hiện lỗi trên VM trước khi sửa, rồi chạy test PASS.

**Tái hiện (trước fix):** test gốc gọi `connector.authenticate(account)` không nguồn token → `TikTokAuthError: cannot authenticate: no auth_code, no TokenSet and no token_provider (Vault) configured — refusing to fabricate a token`. Hành vi connector là ĐÚNG theo `fix_tiktok_auth_fabricated`; lỗi ở TEST.

**Fix — chỉ sửa test, không nới lỏng connector:**
- `tests/test_fix_tiktok_upload_scope.py`: test scope cũ nay cấp token THẬT qua `tokens=TokenSet(access_token='tok-read-only', scopes=['user.info.basic', 'video.list'])` kèm fake transport chỉ chấp nhận đúng Bearer đó trên `/user/info/` (theo mẫu `FakeTikTok` trong `test_fix_tiktok_auth_fabricated.py`), assert token trả về giữ đúng read scopes, không chứa `video.upload`/`video.publish`, và fake đã thấy Bearer header (chứng minh validate live thật). Thêm 2 test: đường `token_provider` (Vault) trả về cùng bảo đảm, và `authenticate()` không nguồn token vẫn raise `TikTokAuthError` (khóa lại hành vi fail-closed mà test cũ vô tình dựa vào việc nó không tồn tại). Các test scope còn lại giữ nguyên, đang xanh.
- `tests/test_fix_tiktok_scope_test_auth.py` (file test riêng của lỗi này, 9 test): `tokens=`/`token_provider` trả token nguyên trạng không viết lại scopes; `/user/info/` 401 thì raise; token hết hạn có `refresh_token` thì refresh thật qua POST `/oauth/token/` (`grant_type=refresh_token`, expiry từ `expires_in` thật); provider trả `None`, token hết hạn không refresh_token, token rỗng đều raise.

**Lưu ý trung thực — hardening whitelist CHƯA nộp trong nhánh này:** phương án hội đồng có "commit riêng thứ hai" là connector thêm kiểm tra whitelist scope trong `authenticate()`/`_validate_token_live`. Tôi KHÔNG nộp `src/connectors/tiktok_connector.py` vì quy tắc bắt buộc của nhánh chỉ cho nộp 2 file test trên và cấm ghi đè file domain khác. Test cho whitelist cũng chưa có trong 2 file này vì chúng sẽ FAIL khi chưa có code connector tương ứng (đã ghi rõ trong docstring file test mới). Cần Team Lead cấp ngoại lệ hoặc một nhánh riêng cho connector thì mới nộp phần hardening đó.

**Kết quả test trên VM (Python 3.12.3, pytest 9.2.0): 48 passed** = 7 scope + 9 test mới + 15 auth fabricated + 17 algo-ml m2. Chỉ nộp 2 file theo quy tắc nhánh fix.

(đã nộp file `tests/test_fix_tiktok_upload_scope.py`)

(đã nộp file `tests/test_fix_tiktok_scope_test_auth.py`)

## File đã nộp

- `tests/test_fix_tiktok_upload_scope.py`
- `tests/test_fix_tiktok_scope_test_auth.py`
