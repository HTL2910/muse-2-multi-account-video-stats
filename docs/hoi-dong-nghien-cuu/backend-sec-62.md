# Sửa lỗi `fix/fix_api_import_delete_assign_audit`: API import dedup, delete cascade có audit, assign-proxy ghi binding

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp fix_api_import_delete_no_dedup_no_audit (systems-arch). Import upsert theo natural key (workspace, platform, platform_user_id), import lặp không tạo account trùng. Delete không xóa cứng khỏi dict: đánh dấu DISABLED, cascade hoặc thu hồi credential_ref, ghi audit append-only. Assign-proxy kiểm tra proxy thuộc workspace của account, ghi ProxyBinding mới và unbound_at cho binding cũ, tuân thủ sticky 1-1. Test: import lặp 2 lần chỉ tạo 1 account; delete ghi audit và credential bị thu hồi; assign proxy khác workspace bị 403.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_api_import_delete_assign_audit` (nhánh `fix/fix_api_import_delete_assign_audit`)**

Đã đọc bản mới nhất trên `main` (bản đã có keyset-cursor HMAC của fix trước — giữ nguyên 100%, không đụng vào) và `src/contracts.py` (đã có sẵn `ProxyBinding` và `AuditLogEntry` nên không cần sửa contract). Chỉ nộp 2 file đúng phạm vi. Tóm tắt bản vá:

- **Import upsert theo natural key:** `POST /accounts/import` tra cứu theo `(workspace_id, platform, platform_user_id)` qua `InMemoryStore.find_account()`. Đã tồn tại → cập nhật các trường mô tả có giá trị (display_name/credential_ref/owner/niche/tags) và trả về trong `updated`; chưa có → tạo mới vào `created`. Response giữ tương thích ngược (`created`, `count` = số tạo mới) và thêm `updated`, `total`. Trùng ngay trong cùng payload cũng chỉ tạo 1 account; account đã soft-delete vẫn tính là đã tồn tại — import lại không tạo bản sao và không tự kích hoạt lại.
- **Delete = soft-delete + cascade thu hồi + audit:** không còn `del store.accounts[...]`. Account chuyển `DISABLED` và ở lại store để truy vết; `credential_ref` bị thu hồi (xoá khỏi account, audit chỉ lưu SHA-256 của ref chứ không lưu ref gốc); binding proxy đang mở bị đóng (`unbound_at`, reason `account_deleted`) và `proxy_binding_id` được gỡ. Mỗi lần xoá ghi 1 bản ghi audit **append-only** vào `store.audit_log` với hash-chain SHA-256 (`append_audit`/`verify_audit_chain` export ở module, redact khoá nhạy cảm trong detail). Xoá lại account đã DISABLED là idempotent: 204, không ghi audit trùng. Xoá account của workspace khác (theo claims) → 403.
- **Assign-proxy ghi ProxyBinding, chặn chéo workspace:** workspace của proxy được ghi nhận từ claims tại `POST /proxies/import` (`store.proxy_workspaces`); khi assign: account lệch workspace của caller → 403, proxy thuộc workspace khác workspace của account → 403 (proxy nạp thẳng vào store không rõ workspace được coi là tài nguyên dùng chung). Sticky 1-1: proxy đang bound active cho account khác → 409; gán lại đúng proxy đang bound → idempotent trả binding cũ; đổi proxy → binding cũ đóng với `unbound_at` + reason `reassigned`, tạo `ProxyBinding` mới, `account.proxy_binding_id` nay trỏ tới **binding id**, response thêm `binding_id`, và ghi audit `proxy.assign`. Gán cho account DISABLED → 409.

Một điểm cần hội đồng lưu ý khi merge: nhánh này dựa trên `main` hiện tại — bản chưa có fix tenant-isolation/RBAC của nhánh `fix/fix_api_tenant_isolation_rbac` — nên hai nhánh sẽ cần merge có giám sát ở `src/api/main.py` (phần assign/delete của nhánh này tương thích với helper `_account_for_tenant`/`_audit` của nhánh kia).

**Kiểm chứng trên VM Linux:** `pytest tests/test_fix_api_import_delete_assign_audit.py -q` → **11 passed** (import lặp 2 lần chỉ 1 account, dedup trong payload, tách workspace, delete ghi audit + credential bị thu hồi + đóng binding, assign proxy khác workspace → 403, sticky 1-1 + reassign đóng binding cũ). Spot-check thêm: `/health` 200, keyset cursor sai chữ ký vẫn 400 — hành vi của fix cursor không đổi.

(đã nộp file `src/api/main.py`)

(đã nộp file `tests/test_fix_api_import_delete_assign_audit.py`)

## File đã nộp

- `src/api/main.py`
- `tests/test_fix_api_import_delete_assign_audit.py`
