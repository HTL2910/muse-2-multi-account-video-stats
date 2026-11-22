# Sửa lỗi `fix/fix_ingestion_normalize_removed`: Ingestion không bịa 0, ưu tiên watch-time giây, phát hiện video xóa qua store

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp fix_ingestion_normalize_silent_zero_and_ + fix_ingestion_removed_detection_not_pers (algo-ml). normalize_raw_metric gán values=0 khi _to_int trả None mà vẫn quality OK nên 'abc' thành views=0 lọt vào KPI, ctr dạng chuỗi bị bỏ qua thành None; YouTube duyệt watchTimeSeconds trước rồi estimatedMinutesWatched sau nên giây bị ghi đè bằng phút*60; _known chỉ sống trong một instance nên pipeline tạo mới mỗi job thì removed_video_ids luôn []. Fix: giá trị không parse được phải đánh dấu missing/PARTIAL (không gán 0 lặng lẽ), parse ctr cả dạng chuỗi, ưu tiên tuyệt đối watchTimeSeconds và chỉ dùng estimatedMinutesWatched khi không có nó; nạp _known từ store/DB (platform_video_id đã biết của account) khi bắt đầu run() và ghi lại sau run(). Test bằng 2 pipeline instance khác nhau cùng store và fixture giá trị hỏng.

## Bài làm

Đã tái hiện cả 4 lỗi trên máy ảo Linux trước khi fix: views='abc' thành 0 với quality OK; ctr chuỗi "0.05" bị bỏ qua thành None; watchTimeSeconds=120 bị ghi đè thành 600 bởi estimatedMinutesWatched=10; pipeline instance mới cùng store thì removed_video_ids luôn [] dù video b đã biến mất.

Đã chạy kiểm thử trên máy ảo Linux (venv pytest 9.2.0, Python 3.12.3): **71 passed** — 17 test mới `test_fix_ingestion_normalize_removed.py` + 17 test `test_algo_ml_m2.py` + 10 test idempotency + 15 test TikTok auth + 12 test proxy pool, không regression.

Thay đổi chính trong `src/ingestion/pipeline.py` (dựa trên `main` như đã đọc, giữ nguyên classify/spike/dedupe):
- Không bịa 0: trường có trong raw nhưng không parse được (`_to_int` trả None) được ghi vào danh sách unparseable và làm metric PARTIAL — số vẫn giữ 0 để schema không vỡ nhưng dashboard phải loại PARTIAL khỏi KPI. `_to_int` cải tiến thêm: parse được chuỗi số có dấu phẩy ("1,234") và "123.0".
- `ctr` parse cả dạng chuỗi: số, chuỗi số ("0.05") và phần trăm ("5%" → 0.05) qua `_to_float()`; ctr hỏng thì PARTIAL thay vì lặng lẽ thành None.
- Watch-time: `watchTimeSeconds` có ưu tiên tuyệt đối; chỉ dùng `estimatedMinutesWatched` (*60) khi không có giá trị giây hợp lệ, và `total_video_watch_time_ms` (Facebook) đổi sang giây; giây = 0 là giá trị thật, không bị fallback đè.
- Phát hiện video xóa qua store: `InMemoryMetricsStore.known_platform_video_ids(account_id)` (alias `known_video_ids`) liệt kê platform_video_id đã có snapshot của account; `run()` nạp `_known` từ store khi bắt đầu (duck-typing để store PG cũng dùng được) rồi mới so với danh sách phát hiện, nên 2 pipeline instance khác nhau cùng store vẫn báo đúng video đã xóa; store append-only nên lần chạy sau vẫn thấy video đó đã xóa.

(đã nộp file `src/ingestion/pipeline.py`)

(đã nộp file `tests/test_fix_ingestion_normalize_removed.py`)

## File đã nộp

- `src/ingestion/pipeline.py`
- `tests/test_fix_ingestion_normalize_removed.py`
