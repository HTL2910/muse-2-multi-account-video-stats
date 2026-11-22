# Sửa lỗi `fix/fix_scheduler_enqueue_fail_open`: enqueue() fail-closed với account/proxy chưa biết trạng thái

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp fix_scheduler_enqueue_fail_open_unknown_ (algo-ml). enqueue() hiện dùng account_status.get(job.account_id, AccountStatus.ACTIVE) nên account chưa đăng ký, sai account_id hoặc đã BANNED nhưng chưa đồng bộ đều được enqueue; proxy_status được khai báo nhưng không bao giờ kiểm tra. Fix: mặc định account chưa biết là REJECTED (hoặc yêu cầu caller cung cấp trạng thái rõ ràng), kiểm tra cả account_status và proxy_status trong enqueue(), đồng bộ account_status từ store thật thay vì dict rỗng mặc định. Test: enqueue account lạ và account BANNED phải bị REJECTED, không vào queue.

## Bài làm

Đã chạy kiểm thử trên máy ảo Linux: `pytest tests/test_fix_scheduler_enqueue_fail_open.py -q` → **12 passed**, `py_compile` đạt. Chỉ sửa file mục tiêu và thêm file test riêng, giữ nguyên toàn bộ phần còn lại của scheduler.

Tóm tắt bản vá `fix_scheduler_enqueue_fail_open`:
- Bỏ mặc định fail-open `account_status.get(..., AccountStatus.ACTIVE)`: account chưa biết trạng thái → `REJECTED` với `last_error="unknown account status - fail-closed"`, không vào queue.
- Kiểm tra cả proxy ở `enqueue()`: proxy suy ra từ `proxy_id` tường minh, `job.payload["proxy_id"/"proxy_binding_id"]` hoặc binding sticky đã đăng ký; proxy chưa rõ trạng thái → REJECTED, `QUARANTINED`/`DEAD` → REJECTED, chỉ `ACTIVE`/`WARMUP` mới qua.
- Đồng bộ trạng thái từ store thật: `register_account()` / `register_proxy()` / `sync_statuses()` / `sync_from_store()` / `sync_account_status()` / `sync_proxy_status()`, hoặc truyền `account_status=`/`proxy_status=` tường minh vào `enqueue()`. Trạng thái đổi sang BANNED sau khi đăng ký sẽ chặn ngay job mới.
- `requeue_dead_letter()` cũng qua cùng cổng fail-closed (account bị ban trong lúc job nằm ở dead-letter thì requeue bị REJECTED, không quay lại queue).

(đã nộp file `src/scheduler/task_scheduler.py`)

(đã nộp file `tests/test_fix_scheduler_enqueue_fail_open.py`)

## File đã nộp

- `src/scheduler/task_scheduler.py`
- `tests/test_fix_scheduler_enqueue_fail_open.py`
