# Sửa lỗi `fix/fix_sqlite_manual_queue_connection_leak`: Đóng xác định connection SQLite của manual queue theo từng thao tác

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp 5 báo cáo trùng (muse fix_browser_sqlite_connection_leak, systems-arch fix_profile_sqlite_close_windows, algo-ml fix_sqlite_manual_queue_connection_leak, backend-sec fix_browser_profile_sqlite_handle_leak, fullstack-ux fix_profile_manual_queue_sqlite_handle_l). Một gốc rễ cho cả 3 test profile đỏ (fingerprint ổn định, proxy mismatch fail-closed, checkpoint vào manual queue): mọi thao tác của SQLiteManualQueue dùng mẫu 'with self._connect() as conn:' nhưng __exit__ của sqlite3.Connection chỉ commit/rollback, KHÔNG close — systems-arch và algo-ml đã đo độc lập trên VM: sau __init__ đã có 1 FD mở tới manual_queue.sqlite3, mỗi thao tác thêm 1 FD (3, 4, 5, 6...), chỉ biến mất sau gc.collect(); trên Linux unlink file đang mở vẫn được nên lỗi tàng hình, trên Windows handle mở chặn unlink nên TemporaryDirectory cleanup ném PermissionError [WinError 32]. Đính chính quan trọng: đề xuất 'thêm SQLiteManualQueue.close() rồi gọi ở manager close / fixture teardown' là KHÔNG ĐỦ — connection rò là temporary mở trong từng thao tác, không còn tham chiếu nào để close() đóng được. Fix đúng: biến _connect() thành contextmanager _session() — mở connection, yield, commit khi thành công / rollback khi lỗi, và LUÔN close() trong finally; mọi call site 'with self._connect() as conn:' giữ nguyên không đổi. Không gọi close() bên trong khối with cũ (đã kiểm chứng: __exit__ sau đó commit trên DB đã đóng và raise ProgrammingError). Không dùng gc.collect() trong test để lấp lỗi. Kiểm chứng: sau chuỗi thao tác như 3 test, không còn FD nào trỏ tới file db (đếm qua /proc/self/fd trên Linux), rồi chạy lại 3 test trên Windows CI phải xanh. Log profile_geo_unresolved (fallback UTC/en-US khi không có sticky proxy country) là hành vi ĐÚNG theo thiết kế — cấm sửa thành mặc định US. Sau khi vá rò rỉ, assertion nào trong 3 test còn đỏ mới tách thành lỗi riêng với traceback đầy đủ.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_sqlite_manual_queue_connection_leak` — BÁO CÁO**

Đã sửa đúng cơ chế hội đồng chốt (không dùng phương án close() ở manager — phương án đó không đủ vì connection rò là connection tạm của từng thao tác, không còn tham chiếu để đóng hộ):

- `SQLiteManualQueue._connect()` chuyển thành **contextmanager**: mở connection → yield → **commit** khi khối `with` thoát bình thường / **rollback** khi có exception → **luôn `close()` trong finally**. Thêm alias `_session = _connect` theo tên gọi trong phương án fix. Cả 9 call site `with self._connect() as conn:` giữ nguyên không đổi; không gọi `close()` bên trong khối `with` cũ (tránh ProgrammingError như systems-arch đã kiểm chứng); không đụng tới hành vi geo (log `profile_geo_unresolved` giữ nguyên là hành vi đúng).

**Kiểm chứng trên VM:**
- Test mới `tests/test_fix_sqlite_manual_queue_connection_leak.py` (4 test): đếm FD qua `/proc/self/fd` — sau `__init__` và sau nhiều vòng enqueue/claim/fail/complete/list, số FD trỏ tới file db luôn về **0** mà không cần `gc.collect()`; exception giữa session vẫn đóng connection **và** rollback (dữ liệu không commit dở); file db xóa được ngay sau thao tác; ngữ nghĩa queue (dedupe, claim tăng attempts, fail quay PENDING rồi FAILED ở max_attempts, bền vững qua instance mới) không đổi.
- Kiểm chứng test không rỗng: chạy lại đúng mẫu cũ (connection thô) thì 4 thao tác rò đúng **4 FD**.
- Regression: 12 test `test_fix_browser_profile_missing.py` vẫn xanh — tổng **16 passed**. Smoke ở tầng manager: `handle_block` enqueue mục captcha, `manual_queue` đọc lại được, 0 FD còn mở.

(đã nộp file `src/browser/profile_manager.py`)

(đã nộp file `tests/test_fix_sqlite_manual_queue_connection_leak.py`)

## File đã nộp

- `src/browser/profile_manager.py`
- `tests/test_fix_sqlite_manual_queue_connection_leak.py`
