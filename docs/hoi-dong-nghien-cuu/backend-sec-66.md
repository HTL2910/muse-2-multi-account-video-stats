# Sửa lỗi `fix/fix_vault_kek_fallback_wipe`: Vault fail-closed khi thiếu KEK hoặc thiếu cryptography, sửa _wipe

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp fix_vault_ephemeral_kek_and_silent_fallb (algo-ml) + fix_vault_ephemeral_kek_data_loss (backend-sec) + fix_vault_wipe_noop_and_aead_fallback (backend-sec). EnvKekProvider tự sinh KEK os.urandom(32) trong RAM khi thiếu VAULT_KEK_B64 nên restart là mất toàn bộ secret; fallback HMAC-stream chạy lặng lẽ thay AES-256-GCM; _wipe(bytearray(dek)) chỉ zero bản sao. Fix: production thiếu KEK thì raise VaultError khi khởi động, không tự sinh; chỉ cho ephemeral KEK khi có cờ DEV_MODE rõ ràng kèm cảnh báo; validate VAULT_KEK_B64 là base64 hợp lệ đúng 32 byte; thiếu thư viện cryptography và không ở DEV_MODE thì raise khi khởi tạo Vault, nếu giữ fallback thì đặt tên/flag record khác hẳn format AES, log warning có cấu trúc và chặn ghi secret mới; giữ DEK dạng bytearray từ đầu và zero chính đối tượng đó trong finally (ghi rõ giới hạn Python/HSM, sửa docstring bỏ cam kết xóa tuyệt đối). Test restart: instance 2 cùng KEK retrieve được, khác KEK phải thất bại rõ ràng.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_vault_kek_fallback_wipe` (nhánh `fix/fix_vault_kek_fallback_wipe`)**

Đã đọc bản mới nhất trên `main` trước khi sửa; chỉ nộp file mục tiêu + file test riêng. Tóm tắt bản vá (giữ nguyên `TokenManager`, `validate_cookie_blob`, `mask_secret`, rotate/delete và toàn bộ hành vi hợp lệ cũ):

- **KEK fail-closed:** `EnvKekProvider` không còn tự sinh `os.urandom(32)` khi thiếu `VAULT_KEK_B64` — thiếu KEK (và không truyền `kek` trực tiếp) thì `raise VaultError` ngay lúc khởi động, kèm thông điệp chỉ rõ cách khắc phục. KEK tạm thời chỉ được sinh khi DEV_MODE rõ ràng (tham số `dev_mode=True` hoặc env `VAULT_DEV_MODE=1`/`DEV_MODE=1`), khi đó `provider.ephemeral is True` và có log warning có cấu trúc (JSON 1 dòng, event `ephemeral_kek_generated`). Biểu thức khởi tạo cũ có lỗi precedence (ternary lồng nhau khó kiểm chứng) cũng được viết lại tường minh.
- **Validate KEK nghiêm:** `VAULT_KEK_B64` được giải mã với `validate=True` (chấp nhận cả alphabet chuẩn và urlsafe) và bắt buộc đúng 32 byte sau giải mã; base64 sai hoặc độ dài sai → `VaultError` ngay. `kek` truyền trực tiếp cũng phải đúng 32 byte.
- **Thiếu `cryptography` thì chặn, không lặng lẽ hạ cấp:** ngoài DEV_MODE, `EncryptedSecretVault.__init__` raise `VaultError`; đường wrap/unwrap `H1` trong `EnvKekProvider` cũng bị chặn tương tự, và `store()` kiểm tra lại tại thời điểm ghi (phòng khi `_HAS_AESGCM` thay đổi sau khởi tạo). Khi DEV_MODE cho phép fallback: vault đặt `insecure_fallback=True`, log warning có cấu trúc (event `insecure_fallback_enabled`), và bản ghi mang định dạng **khác hẳn** — trường mới `EncryptedRecord.alg = "hmac-sha256-stream-dev-fallback"` so với `"aes-256-gcm"`, kèm prefix wrap `H1` thay vì `G1`. `_aead_decrypt` đọc theo `alg` của bản ghi; bản ghi legacy không có `alg` vẫn giải mã được bằng suy luận từ độ dài nonce như trước.
- **Sửa `_wipe`:** lỗi cũ là `_wipe(bytearray(dek))` — chỉ zero bản sao vừa tạo, DEK gốc không hề bị xoá. Nay DEK là `bytearray` ngay từ khi sinh ra trong `store()`, và `retrieve()` cũng chuyển DEK thành `bytearray` ngay sau unwrap; cả hai đều `_wipe(dek)` trên **chính đối tượng đó** trong `finally`. Docstring của `_wipe` và đầu file đã bỏ cam kết xoá tuyệt đối, ghi rõ giới hạn: chỉ xoá được đối tượng đang giữ — các bản sao đã tạo khi tính toán, internals của interpreter, swap/hibernation của OS nằm ngoài tầm kiểm soát; khoá dài hạn phải ở KMS/HSM.
- **Lỗi giải mã rõ ràng:** `InvalidTag` từ AESGCM khi unwrap/decrypt (KEK sai, dữ liệu bị sửa) nay được bọc thành `VaultError` thay vì rò exception của thư viện — điều kiện cần để test restart "khác KEK phải thất bại rõ ràng" kiểm chứng được.

**Kiểm chứng trên VM Linux:** `pytest tests/test_fix_vault_kek_fallback_wipe.py -q` → **10 passed**, gồm test restart đúng spec: instance 2 cùng KEK (store dùng chung) retrieve được, instance khác KEK → `VaultError`; thiếu KEK → raise; base64/độ dài sai → raise; ephemeral chỉ ở DEV_MODE và hai provider ephemeral không đọc được dữ liệu của nhau; mô phỏng thiếu `cryptography` bằng monkeypatch `_HAS_AESGCM` → raise ngoài DEV_MODE, và ở DEV_MODE thì roundtrip được với `alg` fallback + prefix `H1`; test spy trên `_aead_encrypt/_aead_decrypt` xác nhận DEK là `bytearray` và chính đối tượng đó bị zero sau khi dùng. Smoke test thêm: `TokenManager.ensure_fresh` + `validate_cookie_blob` vẫn chạy đúng.

(đã nộp file `src/accounts/vault.py`)

(đã nộp file `tests/test_fix_vault_kek_fallback_wipe.py`)

## File đã nộp

- `src/accounts/vault.py`
- `tests/test_fix_vault_kek_fallback_wipe.py`
