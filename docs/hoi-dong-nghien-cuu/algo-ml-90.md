# Sửa lỗi `fix/fix_scheduler_scheduled_at_lost`: Khôi phục fix scheduled_at bị mất khi merge lên main

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp fix_scheduler_scheduled_at_lost (algo-ml, P0 cao nhất vòng 2, đã tái hiện trên VM: job FETCH_STATS hẹn +6h bị acquire ngay tại T0, job retry sau 429 bị lấy lại ngay lập tức). Bản main hiện tại không còn dấu vết fix: PriorityQueue không nhận clock, không có pop_due/peek_due/next_scheduled_at/pop_any, pop() lấy job bất kể scheduled_at, acquire_job() gọi pop() vô điều kiện; file tests/test_fix_scheduler_scheduled_at.py vắng mặt trên main. Fix: MERGE LẠI nhánh cũ, không viết lại từ đầu — PriorityQueue nhận clock inject được, thêm pop_due(now) quét toàn heap tìm job due tốt nhất theo sort key (không chỉ peek head, vì job VIP tương lai ở đầu heap không được chặn job thường đã due phía sau), pop() bắt buộc due, pop_any() chỉ là escape hatch drain/test; acquire_job() dùng pop_due với now snapshot một lần mỗi lần gọi; finish_job() giữ nguyên cách tính scheduled_at retry (nay mới thực sự được tôn trọng). Khôi phục kèm tests/test_fix_scheduler_scheduled_at.py (8 test: job +6h, VIP tương lai không chặn job due, cadence 15 phút/6 giờ, retry sau 429, pop_any). Merge trong cùng cửa sổ với fix_scheduler_enqueue_dedup (scheduled_at trước, dedup sau) để tránh mất fix lần nữa; kiểm chứng bằng grep xác nhận pop_due tồn tại trên main sau merge và file test xuất hiện trong tests/.

## Bài làm

Nhánh `fix/fix_scheduler_scheduled_at_lost` — khôi phục fix scheduled_at bị mất khi merge (không viết lại từ đầu, merge trên bản main hiện tại gồm cả fix quota + enqueue gate, cả hai giữ nguyên không thay đổi).

**Lỗi đã tái hiện trên VM trước khi sửa (đúng logic file main):** job `FETCH_STATS` hẹn +6h bị `acquire_job` lấy ngay tại T0; job retry sau lỗi 429 (đã đẩy `scheduled_at` vào tương lai) cũng bị lấy lại ngay — backoff vô hiệu, nhịp 15 phút/6 giờ vô nghĩa.

**Fix (khôi phục bản cũ lên main):**
- `PriorityQueue` nhận `clock` inject được; thêm `pop_due(now)` quét **toàn heap** tìm job due tốt nhất theo sort key (không chỉ peek head — job VIP tương lai ở đầu heap không được chặn job thường đã due phía sau); `pop()` bắt buộc due; `pop_any()` chỉ là escape hatch drain/test; thêm `peek_due()`/`peek()`/`next_scheduled_at()`.
- `TaskSchedulerImpl.__init__` đặt `_clock`/`_rng` trước khi tạo queue, mọi queue dùng chung clock của scheduler; `acquire_job()` snapshot `now` một lần rồi lặp `pop_due(now=now)`; `finish_job()` giữ nguyên cách tính `scheduled_at` retry (nay mới thực sự được tôn trọng).
- **Đã sửa một lỗi do chính tay mình suýt tái phạm:** bản nháp đầu của `_as_utc` đặt `from datetime import timezone` trong nhánh `if` làm `timezone` thành biến cục bộ và gây `UnboundLocalError` ngay cả với datetime aware — đã chuyển import lên đầu module và test bắt được trước khi nộp.
- `pop_any()` đã kiểm chứng đúng dòng CI từng bắt trước đây: `item = heapq.heappop(...).job` rồi `self._seen.pop(item.idempotency_key, None)` (không còn `item.job` sai).

**Kiểm chứng trên VM (Python 3.12.3, pytest 9.2.0): 51 passed** = 8 test mới + 8 test scheduled_at cũ + 8 quota + 10 idempotency + 17 algo-ml m2. Kiểm chứng live: `FETCH_STATS` +6h không bị lấy tại T0, retry sau 429 không bị lấy lại ngay. Chỉ nộp 2 file theo quy tắc nhánh fix; fix enqueue-dedup thuộc nhánh riêng sau (scheduled_at trước, dedup sau) nên không trộn vào đây.

(đã nộp file `src/scheduler/task_scheduler.py`)

(đã nộp file `tests/test_fix_scheduler_scheduled_at_lost.py`)

## File đã nộp

- `src/scheduler/task_scheduler.py`
- `tests/test_fix_scheduler_scheduled_at_lost.py`
