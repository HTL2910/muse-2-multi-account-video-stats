# Sửa lỗi `fix/fix_scheduler_enqueue_dedup`: enqueue() trả job gốc kèm cờ dedup thay vì PENDING giả

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp fix_scheduler_enqueue_dedup_protocol (systems-arch). Contracts 1.1.0 khóa: TaskScheduler.enqueue khi gặp job trùng idempotency_key phải trả về job gốc kèm deduplicated=True / duplicate_of=<id gốc>. Nhưng TaskSchedulerImpl.enqueue() gọi self.queues[platform].push(job) mà bỏ qua giá trị trả về — PriorityQueue.push trả False khi key đã có trong _seen (job không vào heap) — rồi vẫn gán job.status=PENDING và trả job đó về, nên caller (API _enqueue) tin job đã xếp hàng trong khi job đã bị loại lặng lẽ (mất job) và không ai nhận được cờ dedup. CollectJob tự sinh key theo bucket phút của scheduled_at nên hai job SYNC cùng video trong cùng một phút là trùng key thật. Fix: trong enqueue(), nếu push() trả False thì tra job gốc từ queue._seen theo idempotency_key, đặt deduplicated=True và duplicate_of=id job gốc và trả về nó thay vì job PENDING giả; nếu push thành công thì giữ hành vi hiện tại. Test: enqueue 2 job cùng key trong cùng phút -> job thứ hai trả về deduplicated=True, queue chỉ chứa 1 job. Merge ngay sau fix_scheduler_scheduled_at_lost trong cùng cửa sổ scheduler.

## Bài làm

Nhánh `fix/fix_scheduler_enqueue_dedup` — `enqueue()` trả job gốc kèm cờ dedup thay vì PENDING giả (contracts 1.1.0). Merge ngay sau `fix_scheduler_scheduled_at_lost` trong cùng cửa sổ scheduler; bản main dùng làm nền đã chứa fix scheduled_at vừa merge.

**Lỗi đã tái hiện trên VM trước khi sửa:** enqueue 2 job `FETCH_STATS` cùng video, cùng phút (cùng `idempotency_key` thật do `CollectJob` sinh key theo bucket phút) — lần 2 trả về job-2 với `status=PENDING`, `deduplicated=False`, trong khi queue chỉ có 1 job và job-2 không hề nằm trong heap. Caller (API `_enqueue`) tin job đã xếp hàng trong khi job đã bị loại lặng lẽ.

**Fix (đúng phương án hội đồng, chỉ trong `enqueue()`):**
- `enqueue()` kiểm tra giá trị trả về của `push()`: False thì tra job gốc qua `PriorityQueue.get_by_idempotency_key()` (accessor mới, không cho caller soi trực tiếp `_seen`), đặt `deduplicated=True`, `duplicate_of=<id gốc>` và trả về job gốc; push thành công thì giữ nguyên hành vi cũ (PENDING, trả job đó).
- Nhánh phòng thủ cho trường hợp không thể xảy ra (push False nhưng tra không thấy gốc): trả job vào kèm `deduplicated=True` thay vì PENDING giả — ít nhất caller biết job đã bị dedup.
- Cổng fail-closed chạy trước push nên không đổi: account chưa biết vẫn REJECTED, không bị nhầm thành dedup.
- **Follow-up đã ghi rõ trong code (chưa sửa ở nhánh này, ngoài phạm vi đã thống nhất):** `requeue_dead_letter()` cũng bỏ qua giá trị trả về của `push()` — cùng dạng mất job lặng lẽ nếu key đã có trong queue — đã để lại `TODO` tại chỗ để fix tiếp theo xử lý như `enqueue()`.

**Kiểm chứng trên VM (Python 3.12.3, pytest 9.2.0): 58 passed** = 7 test mới + 8 scheduled_at-lost + 8 scheduled_at cũ + 8 quota + 10 idempotency + 17 algo-ml m2. Kiểm chứng live: lần enqueue thứ hai trả về đúng `job-1` với `deduplicated=True`, `duplicate_of=job-1`, queue vẫn 1 job.

(đã nộp file `src/scheduler/task_scheduler.py`)

(đã nộp file `tests/test_fix_scheduler_enqueue_dedup.py`)

## File đã nộp

- `src/scheduler/task_scheduler.py`
- `tests/test_fix_scheduler_enqueue_dedup.py`
