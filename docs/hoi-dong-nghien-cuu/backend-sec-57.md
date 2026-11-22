# Sửa lỗi `fix/fix_api_tenant_isolation_rbac`: API cách ly tenant theo JWT claims, auth thật cho REST và WebSocket

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp fix_api_tenant_isolation_and_ws_no_auth (algo-ml) + fix_api_tenant_isolation_rbac (fullstack-ux) + fix_api_auth_fake_job_cors (systems-arch). Suy ra workspace_id và role duy nhất từ JWT claims đã verify (bỏ workspace_id khỏi body create_account và khỏi query list_accounts, lệch claims thì 403); mọi endpoint get/update/delete/sync/list video/metrics/job đều lọc theo workspace của claims; gắn require_permission/has_permission của src/security/rbac_audit.py theo từng thao tác và ghi AuditChain cho SENSITIVE_ACTIONS; triển khai /auth/login thật bằng hash_password/verify_password + verify_totp thay vì 501; thêm auth cho /ws/events bằng token ở handshake; thu hẹp CORS về origin cụ thể theo env thay vì '*'; fail-fast khi thiếu JWT_SECRET lúc khởi động; khi không có scheduler thì trả 503 thay vì tạo job PENDING giả.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_api_tenant_isolation_rbac` (nhánh `fix/fix_api_tenant_isolation_rbac`)**

Đã đọc bản mới nhất trên `main` của `src/api/main.py` và `src/security/rbac_audit.py` trước khi sửa; chỉ nộp 2 file đúng phạm vi nhánh. Tóm tắt bản vá:

- **Tenant isolation từ claims:** `workspace_id` + `role` chỉ lấy từ JWT đã verify (chuẩn hoá trong `_normalize_claims`). `AccountCreate.workspace_id` chuyển thành optional — chỉ còn dùng để phát hiện lệch claims (có mặt + khác claims → 403), không còn là nguồn sự thật; `list_accounts`/`list_groups`/`list_tags` bỏ `workspace_id: Query(...)` bắt buộc, tham số lệch claims → 403. Mọi endpoint get/update/delete/sync/list videos/metrics/jobs đều lọc theo workspace của claims (RLS qua `require_permission(..., resource_workspace_id=...)` của approach_16 → 403 khi chéo tenant).
- **RBAC + audit:** gắn `Permission` theo từng thao tác (view/create/delete/sync, proxy.view/assign/import); các action thuộc `SENSITIVE_ACTIONS` (`account.create`, `account.delete`, `proxy.assign`, `proxy.import`, `auth.login`, `auth.login_failed`, `auth.2fa_failed`) được ghi vào `AuditChain` (phơi qua `app.state.audit`). JWT thật thiếu claim `role` → mặc định VIEWER (fail-closed); `auth_checker` inject cho test/dev thì mặc định ADMIN như ngữ nghĩa bypass cũ.
- **`/auth/login` thật:** thay 501 bằng `verify_password` + `verify_totp` của rbac_audit (TOTP bắt buộc với ADMIN và mọi user có `totp_secret`; ADMIN chưa enroll TOTP → 403), kèm `LoginRateLimiter`, lỗi sai mật khẩu/user lạ đều là 401 "invalid credentials" chung (không lộ user tồn tại), thành công mới ký JWT HS256 chứa `sub`/`workspace_id`/`role`. User store truyền qua tham số `users=` của `create_app` hoặc env `API_USERS_JSON`.
- **WebSocket:** `/ws/events` auth ngay ở handshake (`?token=` hoặc header `Authorization`); token thiếu/sai → đóng 4401, thiếu quyền → 4403. `EventHub` ghi workspace theo từng client và broadcast có `workspace_id` chỉ tới client cùng workspace.
- **CORS:** bỏ `*`, chỉ nhận origin cụ thể từ tham số `cors_origins` hoặc env `CORS_ALLOWED_ORIGINS` (mặc định `http://localhost:3000`); cấu hình `*` bị lọc bỏ.
- **Fail-fast:** `create_app()` không có `JWT_SECRET` và không có `auth_checker` → `RuntimeError` ngay lúc khởi động. Bản `app` module-level vẫn import được (fallback `auth_checker` trả 503 cho mọi request) để không phá test import — hành vi fail-closed, không còn 500 mơ hồ.
- **Không còn job giả:** `_enqueue` khi `scheduler is None` → 503 và không ghi job PENDING vào store; khi có scheduler, trạng thái thật của job (kể cả REJECTED từ cổng fail-closed của approach_09) được phản ánh, và nếu scheduler hỗ trợ `register_account` thì API đồng bộ account/proxy status thật vào trước khi enqueue. Ngoài ra `decode_cursor` nay từ chối offset âm (khép luôn lỗi `fix_api_cursor_offset_unstable` đã nêu ở vòng audit).

**Kiểm chứng trên VM Linux:** venv riêng (fastapi 0.165) — `pytest tests/test_fix_api_tenant_isolation_rbac.py -q` → **16 passed**; `py_compile` cả 2 file OK. Lưu ý minh bạch: khi chạy test local, `src/security/rbac_audit.py` được thay bằng stub nội bộ bám đúng API approach_16 (chỉ để test, không nộp); trên repo thật file này dùng bản rbac_audit chính thức qua import path thứ nhất, và vẫn còn fallback cùng ngữ nghĩa nếu cả hai import path đều lỗi.

(đã nộp file `src/api/main.py`)

(đã nộp file `tests/test_fix_api_tenant_isolation_rbac.py`)

## File đã nộp

- `src/api/main.py`
- `tests/test_fix_api_tenant_isolation_rbac.py`
