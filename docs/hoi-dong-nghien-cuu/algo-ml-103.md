# Sửa lỗi `fix/fix_scheduler_enqueue_dedup_protocol`: enqueue() trả job gốc kèm cờ dedup thay vì job PENDING ma

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp fix_scheduler_enqueue_dedup_protocol (systems-arch) với nhánh fix_scheduler_enqueue_dedup đã nộp (algo-ml). Lỗi CODE độc lập với test dead-letter: enqueue() trên main vẫn là bản cũ — gán job.status = PENDING, gọi push(job) rồi vứt bỏ giá trị trả về False khi trùng idempotency_key — trong khi changelog contracts 1.1.0 đã khóa: enqueue trên key trùng PHẢI trả về job gốc kèm deduplicated=True / duplicate_of=<id gốc>. Hậu quả production: caller theo dõi một job PENDING ma không bao giờ chạy, trạng thái job và queue lệch nhau (test m2 đã chứng minh: j2 bị loại khỏi queue nhưng vẫn được báo PENDING). Hành động chính là MERGE nhánh fix_scheduler_enqueue_dedup đã nộp, không viết fix mới — nhưng trước khi giao việc, grep một dòng enqueue() trên main để xác minh merge chưa xảy ra sau khi systems-arch đọc main (nếu đã merge thì cụm này đóng). Kỹ thuật: PriorityQueue expose tra cứu job gốc theo key (get_by_key); trong enqueue(), nếu push() trả False thì trả về ĐÚNG object gốc đã queue kèm deduplicated=True và duplicate_of=id gốc — không dùng dataclasses.replace() (tạo bản sao tách rời khỏi job trong queue, sai tinh thần 'trả về job gốc'). Kèm test: enqueue 2 job cùng key trong cùng phút -> job thứ hai trả về là bản gốc với deduplicated=True, queue chỉ chứa 1 job. Kiểm chứng manifest: file test của fix phải xuất hiện trong tests/ trên main. Merge TRƯỚC fix test dead-letter để assert dedup trong test đó khớp hành vi mới.

## Bài làm

Đã xác minh trên `main`: `enqueue()` vẫn là bản cũ (bỏ qua giá trị trả về của `push()`), tức merge chưa xảy ra — đúng như hội đồng đã thống nhất, tôi merge nhánh `fix_scheduler_enqueue_dedup` đã nộp, không viết fix mới. Thay đổi duy nhất so với `main`: `PriorityQueue.get_by_idempotency_key()`, nhánh dedup trong `enqueue()` (trả về đúng object gốc đã queue, không dùng `dataclasses.replace()`), đoạn docstring ghi nhận fix, và ghi chú TODO cho `requeue_dead_letter()`. Toàn bộ phần còn lại giữ nguyên bản `main`.

Kiểm chứng trên VM (Python 3.12.3, pytest 9.2.0): **136 passed** toàn bộ suite local; scenario gốc của test m2 (`j1`/`j2` cùng key): `enqueue(j2)` trả về `j1` (`is j1: True`), `deduplicated=True`, `duplicate_of=j1`, queue chỉ còn 1 job.

(đã nộp file `src/scheduler/task_scheduler.py`)

(đã nộp file `tests/test_fix_scheduler_enqueue_dedup_protocol.py`)

## File đã nộp

- `src/scheduler/task_scheduler.py`
- `tests/test_fix_scheduler_enqueue_dedup_protocol.py`
