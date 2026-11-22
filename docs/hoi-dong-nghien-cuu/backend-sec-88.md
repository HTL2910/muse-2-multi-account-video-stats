# Sửa lỗi `fix/fix_api_tenant_isolation_missing`: Merge có giám sát nhánh API tenant lên main (kèm fake-job và auth surface)

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp fix_api_tenant_isolation_missing_on_main (backend-sec) + fix_api_fake_enqueue_pending + fix_api_auth_surface (systems-arch) thành MỘT lỗi gốc theo phán quyết backend-sec: bản api/main.py trên main là baseline của nhánh import/delete, chưa từng chứa nhánh tenant; job PENDING giả, CORS '*', login 501 và JWT thiếu secret trả 500 đều là triệu chứng của cùng một nhánh chưa merge (nhánh đó đã xử lý cả bốn). Tuyệt đối không vá lẻ từng triệu chứng trên baseline cũ. Fix: merge có giám sát hai nhánh fix_api_tenant_isolation_rbac + fix_api_import_delete_assign_audit trên cùng một bản src/api/main.py (giữ helper require_permission của nhánh tenant làm nền, đặt logic import/delete/assign lên trên). Tiêu chí chấp nhận đầy đủ: (1) workspace/role chỉ suy ra từ JWT claims đã verify, client gửi workspace lệch claims -> 403; mọi endpoint kể cả /ws/events lọc theo workspace của claims; (2) không có scheduler thì _enqueue trả 503 thay vì ghi job PENDING giả, chỉ ghi job khi scheduler.enqueue() thật sự nhận job; (3) CORS allow_origins lấy từ env (mặc định chỉ frontend origin), không '*'; (4) /auth/login triển khai thật qua approach_16 (verify argon2id + 2FA TOTP bắt buộc cho ADMIN rồi ký JWT) hoặc xóa endpoint và ghi rõ trong OpenAPI/docs rằng token cấp ngoài API; (5) thiếu JWT_SECRET thì fail-fast lúc create_app (raise lúc khởi động), không 500 từng request. Phối hợp với fullstack-ux (id 5) trong cùng đợt merge: sửa DashboardAccounts.tsx bỏ việc gửi workspace_id từ URL (mặc định 'default') — workspace suy ra từ phiên đăng nhập. Bổ sung file test tenant vào tests/. Test bắt buộc: JWT workspace A đọc/sửa dữ liệu workspace B -> 403 ở mọi endpoint kể cả /ws/events; create_app không scheduler -> POST sync trả 503 và store.jobs trống.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_api_tenant_isolation_missing` — BÁO CÁO MERGE CÓ GIÁM SÁT**

Đã thực hiện đúng phán quyết vòng tranh luận: **không vá lẻ triệu chứng trên baseline cũ**, mà merge nhánh `fix_api_tenant_isolation_rbac` lên trên baseline hiện tại của main (bản keyset-cursor + `fix_api_import_delete_assign_audit` — tôi đã đối chiếu bản main trả về khớp từng đoạn với bản làm việc vòng 7 của tôi trước khi merge). Kết quả là MỘT bản `src/api/main.py` duy nhất (1077 dòng) thỏa cả 5 tiêu chí chấp nhận:

1. **Tenant isolation:** workspace/role chỉ từ JWT claims đã verify (`_normalize_claims`); `AccountCreate.workspace_id` chuyển thành optional chỉ để phát hiện lệch (có mặt + khác claims → 403); mọi endpoint — gồm `/videos/{id}/metrics` (đối chiếu qua account sở hữu video/dòng metric), `/jobs`, `/groups`, `/tags` và `/ws/events` (auth ngay ở handshake, đóng 4401/4403, broadcast lọc theo workspace của từng client qua EventHub mới) — đều lọc theo workspace của claims.
2. **Không job giả:** `_enqueue` khi không có scheduler trả **503** và `store.jobs` giữ nguyên trống; chỉ ghi job khi `scheduler.enqueue()` thật sự nhận (kèm đồng bộ `register_account` cho scheduler fail-closed và trường `workspace_id` trong bản ghi job).
3. **CORS:** `allow_origins` từ env `CORS_ALLOWED_ORIGINS`, mặc định chỉ `http://localhost:3000`, lọc bỏ `*` kể cả khi cấu hình sai.
4. **Login thật:** `/auth/login` verify password (hash của approach_16) + TOTP bắt buộc cho ADMIN, `LoginRateLimiter` chống dò mật khẩu, lỗi chung 401, thành công mới ký JWT mang claims workspace/role.
5. **Fail-fast:** `create_app()` thiếu cả JWT_SECRET lẫn auth_checker thì raise `RuntimeError` lúc khởi động; app module-level chưa cấu hình vẫn import được nhưng fail-closed 503 cho mọi request — không còn 500 từng request.

**Quyết định merge đáng chú ý:** hai nhánh có hai hệ audit — tôi giữ **một** hệ duy nhất là hash-chain `append_audit`/`verify_audit_chain` của baseline (bản tenant dùng `AuditChain` riêng bị loại bỏ), các thao tác nhạy cảm của lớp tenant ghi vào cùng chain đó; thêm adapter chỉ-đọc `_AuditView` cho `app.state.audit.entries()/verify_chain()` để tương thích. Logic import UPSERT, soft-delete và assign-proxy binding/sticky 1-1 của nhánh import/delete giữ nguyên từng dòng, chỉ đặt dưới guard tenant/RBAC (`_account_for_tenant` + `require_permission`).

**Kiểm chứng trên VM: 40 test passed** — bộ test tenant vòng 6 (16) + bộ test import/delete/assign vòng 7 (11) chạy nguyên vẹn trên bản merge, cộng file test mới của nhánh này (13 test): cross-workspace 403 ở mọi endpoint kể cả metrics/jobs và lọc event WS theo tenant; sync không scheduler → 503 và `store.jobs` rỗng; CORS không `*`; fail-fast khi thiếu secret; luồng login ADMIN (thiếu TOTP → 401, đúng TOTP → JWT, dùng JWT đọc chéo workspace → 403); JWT không role claim → mặc định VIEWER; và regression import-upsert/soft-delete/assign-binding/audit-chain sau merge.

Hai file bàn giao đính kèm trực tiếp từ VM để đảm bảo nguyên vẹn từng byte (file main dài 1077 dòng, tránh nguy cơ cắt/gõ lại thủ công):

(đã tải file từ Linux VM `/home/hatch/workspace/council_fix10/src/api/main.py` → `src/api/main.py`)
(đã tải file từ Linux VM `/home/hatch/workspace/council_fix10/tests/test_fix_api_tenant_isolation_missing.py` → `tests/test_fix_api_tenant_isolation_missing.py`)

Phần phối hợp frontend (bỏ `workspace_id` từ URL trong DashboardAccounts.tsx) thuộc fullstack-ux trong cùng đợt merge như phương án đã ghi — không nằm trong hai file được giao của nhánh này. Ghi nhận thêm: `tests/test_fix_browser_profile_missing.py` đã xuất hiện trong danh sách main — nhánh browser profile nộp ở vòng trước đã lên main thành công.

(đã nộp file `src/api/main.py`)

## File đã nộp

- `src/api/main.py`
