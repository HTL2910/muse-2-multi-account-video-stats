# Sửa lỗi `fix/fix_scheduler_quota_proxy_per_account`: Quota chỉ trừ một lần và triển khai thật lớp per-account

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp fix_proxy_quota_off_by_one (systems-arch, đã kiểm chứng VM) + fix_scheduler_quota_proxy_offbyone_and_p (algo-ml). acquire_job gọi acquire_proxy_slot() rồi allow() lại kiểm tra _proxy_running >= limit nên giới hạn thực luôn bằng cấu hình trừ 1 (limit=1 thì không job nào chạy); _account_buckets được tạo nhưng allow() không dùng. Fix: một điểm trừ duy nhất — kiểm tra cả 3 lớp (account/proxy/platform) trước, chỉ tăng counter khi quyết định chạy (try_acquire nguyên tử + release đối xứng); triển khai thật per-account token bucket trong allow(). Test với per_proxy_concurrency=1 và =2, và 2 account khác nhau vượt quota riêng.

## Bài làm

Đã chạy kiểm thử trên máy ảo Linux (venv pytest 9.2.0, Python 3.12.3): **35 passed** — 8 test mới `test_fix_scheduler_quota_proxy_per_account.py` + 10 test `test_fix_idempotency_hourly_bucket.py` + 17 test cũ `test_algo_ml_m2.py`, không regression.

Thay đổi chính trong `src/scheduler/task_scheduler.py` (dựa trên `main` như đã đọc, không gộp fix `scheduled_at` của nhánh khác):
- Một điểm trừ duy nhất: `QuotaManager.try_acquire()` — kiểm tra cả 3 lớp (account / proxy / platform) trước dưới lock, rồi mới trừ 1 token account + 1 token platform và tăng `_proxy_running`; thất bại ở lớp nào cũng không trừ gì. `acquire_job()` chỉ gọi `try_acquire()`, không gọi riêng `acquire_proxy_slot()` rồi `allow()` nữa — đó là gốc lỗi off-by-one (limit=1 thì không job nào chạy, limit=2 chỉ chạy được 1).
- `allow()` đổi thành peek không tác dụng phụ: không trừ token, không tăng proxy, chỉ để kiểm tra trước khi quyết định.
- Lớp per-account là token bucket thật: mỗi account 1 bucket riêng theo `per_account_quota_per_day` (mặc định bằng quota platform; muốn giới hạn riêng thì truyền dict nhỏ hơn). `acquire_proxy_slot()` giữ lại để tương thích ngược cho caller tự quản lý slot, có ghi rõ không dùng chung với `try_acquire()`.
- `finish_job()` vẫn giải phóng đúng 1 slot proxy cho mỗi lần `try_acquire()` thành công; token quota đã dùng không hoàn lại.

(đã nộp file `src/scheduler/task_scheduler.py`)

(đã nộp file `tests/test_fix_scheduler_quota_proxy_per_account.py`)

## File đã nộp

- `src/scheduler/task_scheduler.py`
- `tests/test_fix_scheduler_quota_proxy_per_account.py`
