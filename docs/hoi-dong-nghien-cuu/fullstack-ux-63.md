# Sửa lỗi `fix/fix_audit_chain_thread_safety`: AuditChain thread-safe và trả bản sao, rõ tamper-evident

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_audit_chain_thread_safety (fullstack-ux). AuditChain.append() đọc prev_hash từ phần tử cuối rồi append mà không có lock nên 2 writer đồng thời tạo hai entry cùng prev_hash; entries() trả chính các object AuditLogEntry mutable nên 'append-only bất biến' chỉ là quy ước. Theo phản biện fullstack-ux, hash-chain là cơ chế phát hiện sửa đổi (tamper-evident), không phải ngăn sửa trong RAM. Fix: bọc append bằng threading.Lock (production dùng DB transaction + advisory lock theo workspace); entries() trả deep copy hoặc bản ghi frozen; ép append-only ở PostgreSQL bằng trigger cấm UPDATE/DELETE. Phần import của báo cáo này đã gộp vào fix_import_dual_contracts, không sửa riêng ở đây.

## Bài làm

Đã kiểm thử trên máy ảo (stub contracts v1.0.0), chạy 3 lần liên tiếp đều PASS, gồm: 8 thread × 50 append thả đồng thời bằng Barrier — chuỗi 400 entry không fork (đúng một GENESIS, mọi prev_hash duy nhất và nối đúng thứ tự, `verify_chain()` True); sửa entry lấy từ `entries()` và cả object trả về từ `append()` đều không đụng được vào bản ghi gốc; sửa thẳng vào bộ nhớ trong (mô phỏng bypass/rogue UPDATE) thì `verify_chain()` trả False — đúng ngữ nghĩa tamper-evident. Thay đổi: `append()` gộp đọc prev_hash + tính hash + append vào một critical section dưới `threading.Lock` (docstring ghi rõ production thay bằng DB transaction + `pg_advisory_xact_lock` theo workspace), `append()`/`entries()` trả deep copy, và bổ sung hằng `AUDIT_APPEND_ONLY_SQL` chứa trigger PostgreSQL cấm UPDATE/DELETE để ép append-only ở tầng lưu trữ. Chỉ nộp file mục tiêu + file test riêng.

(đã nộp file `src/security/rbac_audit.py`)

(đã nộp file `tests/test_fix_audit_chain_thread_safety.py`)

## File đã nộp

- `src/security/rbac_audit.py`
- `tests/test_fix_audit_chain_thread_safety.py`
