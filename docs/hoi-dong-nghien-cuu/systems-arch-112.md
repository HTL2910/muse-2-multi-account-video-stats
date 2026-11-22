# Sửa lỗi `fix/fix_monitor_golden_killswitch_prometheus`: Merge nhánh monitor: golden unwrap, kill-switch ngưỡng riêng, Prometheus TYPE

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_monitor_golden_unmerged (systems-arch). Bản monitor.py trên main vẫn là bản trước fix theo phần đọc toàn file của systems-arch: dùng RISK_STOP_THRESHOLD (0.9) làm ngưỡng kill-switch — ngưỡng cao tới mức gần như không bao giờ kích hoạt, và về ngữ nghĩa là trộn hai miền (ngưỡng rủi ro account 0.9 dùng cho tỉ lệ lỗi crawl là sai khái niệm); không có unwrap_fixture_items đệ quy nên fixture TikTok lồng nhau {data:{videos:[...]}} chạy GoldenRunner ra 0 video; prometheus_text() không gom nhóm theo tên metric nên '# TYPE' lặp lại. Lưu ý xác minh (algo-ml): phần ngưỡng 0.9 khớp trích đoạn vòng 3 (monitor.py đang import đúng hằng đó), nhưng phần unwrap và trùng TYPE chưa xác minh được từ các trích đoạn trong tay — nên người nhận fix đọc monitor.py trên main trước khi merge, không merge theo trí nhớ. Fix: merge nhánh fix_monitor_golden_killswitch_prometheus đã nộp — unwrap đệ quy (data là dict có 'videos' thì items=data['videos'], data là list thì giữ nguyên, lồng nhau thì unwrap tiếp); tách ngưỡng riêng KILL_SWITCH_ERROR_RATE_THRESHOLD=0.4 (cấu hình được 0.3-0.5); thêm kill_switch_gate()/check_kill_switch() và nối vào TaskSchedulerImpl.enqueue()/acquire_job() (phần nối scheduler hạ cánh sau khi các fix scheduler vòng này merge xong để tránh xung đột); gom '# TYPE' một lần cho mỗi tên metric. Nghiệm thu: test_fix_monitor_golden_killswitch_prometheus.py có mặt trên main và chạy xanh với fixture TikTok thật (1 video, play_count 1000).

## Bài làm

**systems-arch — COUNCIL_FIX_WORK — `fix/fix_monitor_golden_killswitch_prometheus`**

Nộp đúng 2 file: `src/observability/monitor.py` + `tests/test_fix_monitor_golden_killswitch_prometheus.py`. Đã đọc toàn file trên main trước khi merge (đúng lưu ý của algo-ml): bản main vẫn là bản trước fix — dual import, kill-switch mặc định `RISK_STOP_THRESHOLD` (0.9), `GoldenRunner.run` dùng `raw.get("data", [])`, `prometheus_text()` in `# TYPE` trước mỗi dòng label. Cả ba lỗi đều xác minh được từ bản đọc này, không merge theo trí nhớ.

**1. File code — chỉ sửa đúng 3 nhóm đã chốt, mọi thứ khác giữ nguyên từng byte so với main:**
- **Golden unwrap:** thêm `unwrap_fixture_items()` đệ quy (list giữ nguyên; dict có `videos`/`items` là list thì lấy list đó; còn lại đệ quy vào `data`; shape khác → `[]`) và `GoldenRunner.run` dùng nó thay cho `raw.get("data", [])`. Với fixture TikTok thật `{"data": {"videos": [...]}}`, bản cũ cho `len=3` (số key của dict `data`) rồi `items[0]` KeyError; bản mới ra đúng 1 video.
- **Kill-switch:** tách ngưỡng riêng `KILL_SWITCH_ERROR_RATE_THRESHOLD = 0.4` (cấu hình được 0.3–0.5) thay cho 0.9 — 0.9 nghĩa là đợi tới khi 9/10 job lỗi mới dừng, và không nơi nào kiểm tra `tripped` nên là dead code. Thêm `kill_switch_gate()` và `Monitor.check_kill_switch()` / `error_rate` / `kill_switch=` tiêm được vào `Monitor.__init__`. `RISK_STOP_THRESHOLD` được bỏ khỏi import (không còn chỗ nào dùng).
- **Prometheus:** `prometheus_text()` gom counter theo tên metric, in `# TYPE <tên> counter` một lần cho mỗi tên rồi mới in các dòng label; gauge giữ nguyên.

**2. File test (10 test, hiệu chỉnh theo fixture TikTok thật — 1 video, `stats` lồng trong `"stats"`, play_count 1000 / digg 120 / comment 15 / share 8 / collect 20):**
- Unwrap đủ shape (list, `data` list, TikTok `data.videos`, `videos` trần, `data.data` lồng, shape lạ → `[]`).
- GoldenRunner với shape fixture thật (file tạm) và với fixture thật trong repo nếu có (không skip trên main): count đúng thì pass, count sai thì fail đúng thông điệp `video count 1 != expected 2`.
- Kill-switch: ngưỡng mặc định 0.4; trip ở 50% lỗi (10 ok + 10 lỗi, min_samples 20); không trip ở 20% lỗi; gate reject khi tripped và allow khi chưa; `Monitor.record_job` nuôi kill-switch và gate đổi trạng thái sau `reset()`.
- Prometheus: `# TYPE` đúng 1 lần cho mỗi tên metric, đủ 3 dòng `jobs_total{...}` và 2 dòng `alerts_total{...}`, rỗng thì ra chuỗi rỗng.

**Kiểm chứng trên VM (chưa chạy trên main — kết quả thật để CI):** replica `~/workspace/fix-mon/` với fixture TikTok thật copy từ main (đã đối chiếu từng byte với bản vừa đọc): **10 passed**. Kiểm tra âm tính: hoàn nguyên `GoldenRunner.run` về `raw.get("data", [])` và `prometheus_text` về bản cũ → đúng 3 test đỏ (cả hai test golden + test TYPE), 7 passed.

**Ngoài phạm vi nhánh này:** nối `kill_switch_gate()` / `check_kill_switch()` vào `TaskSchedulerImpl.enqueue()` / `acquire_job()` — hạ cánh sau khi các fix scheduler vòng này merge xong để tránh xung đột (đã ghi trong docstring của file). Dual import contracts trong monitor.py thuộc PR import-residual, không sửa ở đây.

(đã nộp file `src/observability/monitor.py`)

(đã nộp file `tests/test_fix_monitor_golden_killswitch_prometheus.py`)

## File đã nộp

- `src/observability/monitor.py`
- `tests/test_fix_monitor_golden_killswitch_prometheus.py`
