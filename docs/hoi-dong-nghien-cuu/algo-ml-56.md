# Sửa lỗi `fix/fix_scheduler_scheduled_at`: Scheduler chỉ chạy job đã tới hạn scheduled_at

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 2_

## Việc được giao

Gộp fix_scheduler_ignores_scheduled_at (systems-arch, đã kiểm chứng VM) + fix_scheduler_future_jobs_run_now (algo-ml) + fix_scheduler_ignores_scheduled_at (fullstack-ux). PriorityQueue/acquire_job chỉ trả job khi scheduled_at <= now (peek heap, job đầu chưa tới hạn thì trả None và giữ lại trong queue); áp dụng tương tự cho job retry sau backoff. Test: job FETCH_STATS scheduled +6h không được acquire trước 6h; job retry sau 429 không được acquire trước delay; video mới 15 phút và video cũ 6 giờ giữ đúng cadence.

## Bài làm

Nguyên nhân lỗi CI: ở bản nộp trước, dòng trong `pop_any()` bị chép sai thành `self._seen.pop(item.job.idempotency_key, None)` trong khi `item` ở đó đã là `CollectJob` (vì `item = heapq.heappop(self._heap).job`), nên `item.job` không tồn tại.

Bản trên máy ảo của tôi vốn đã đúng dòng đó (`item.idempotency_key`) và đã pass, lỗi chỉ nằm ở khâu chép lại khi nộp. Đã kiểm tra lại toàn file trên máy ảo: `grep` chỉ còn `item.job` ở `pop_due()` — nơi `item` là `_Queued` nên `item.job` là đúng.

Chạy lại trên máy ảo Linux (venv pytest 9.2.0, Python 3.12.3): **35 passed** — 8 test mới `test_fix_scheduler_scheduled_at.py` + 10 test `test_fix_idempotency_hourly_bucket.py` + 17 test cũ `test_algo_ml_m2.py`.

Nộp lại trọn 2 file, chỉ khác bản trước ở đúng 1 dòng `pop_any()` đã sửa.

(đã nộp file `src/scheduler/task_scheduler.py`)

(đã nộp file `tests/test_fix_scheduler_scheduled_at.py`)

## File đã nộp

- `src/scheduler/task_scheduler.py`
- `tests/test_fix_scheduler_scheduled_at.py`
