# Sửa lỗi `fix/fix_scheduler_scheduled_at`: Scheduler chỉ chạy job đã tới hạn scheduled_at

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp fix_scheduler_ignores_scheduled_at (systems-arch, đã kiểm chứng VM) + fix_scheduler_future_jobs_run_now (algo-ml) + fix_scheduler_ignores_scheduled_at (fullstack-ux). PriorityQueue/acquire_job chỉ trả job khi scheduled_at <= now (peek heap, job đầu chưa tới hạn thì trả None và giữ lại trong queue); áp dụng tương tự cho job retry sau backoff. Test: job FETCH_STATS scheduled +6h không được acquire trước 6h; job retry sau 429 không được acquire trước delay; video mới 15 phút và video cũ 6 giờ giữ đúng cadence.

## Bài làm

Đã chạy kiểm thử trên máy ảo Linux (venv pytest 9.2.0, Python 3.12.3): **35 passed** — 8 test mới `test_fix_scheduler_scheduled_at.py` + 10 test `test_fix_idempotency_hourly_bucket.py` + 17 test cũ `test_algo_ml_m2.py`, không regression.

Thay đổi chính trong `src/scheduler/task_scheduler.py`, chỉ trong phạm vi nhánh này:
- `PriorityQueue.pop_due(now)` / `pop()` chỉ trả job có `scheduled_at <= now`; job tương lai giữ nguyên trong queue. Vì heap sắp theo (priority, deadline, FIFO) nên `pop_due()` quét toàn heap để tìm job đã tới hạn tốt nhất, thay vì chỉ peek đầu heap — job VIP tương lai không chặn được job thường đã tới hạn ở sau.
- Thêm `peek_due()`, `peek()`, `next_scheduled_at()` và `pop_any()` (chỉ dùng khi caller biết rõ, không dùng trong đường chạy worker). Queue nhận clock tiêm được, chung clock với scheduler để test đóng băng thời gian.
- `TaskSchedulerImpl.acquire_job()` dùng `pop_due()` với `now` chụp một lần đầu lần gọi; `finish_job()` không đổi logic, nhưng job retry sau backoff giờ thật sự không được acquire cho tới khi tới `scheduled_at` mới.
- Hai lỗi test ban đầu đã sửa ở phía test (không sửa code để test xanh): 2 job cùng video + cùng phút bị dedup đúng nghĩa theo contracts v1.1.0, và lock per-account của SYNC_VIDEOS phải được giải phóng bằng `finish_job(ok=True)` trước khi FETCH_STATS cùng account được acquire.

(đã nộp file `src/scheduler/task_scheduler.py`)

(đã nộp file `tests/test_fix_scheduler_scheduled_at.py`)

## File đã nộp

- `src/scheduler/task_scheduler.py`
- `tests/test_fix_scheduler_scheduled_at.py`
