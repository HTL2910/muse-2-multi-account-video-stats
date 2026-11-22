# Sửa lỗi `fix/fix_monitor_golden_killswitch_prometheus`: Khôi phục nhánh monitor: golden unwrap, kill-switch nối scheduler, Prometheus TYPE

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_monitor_golden_unwrap + fix_monitor_killswitch_dead_threshold + fix_monitor_prometheus_duplicate_type (systems-arch) — đúng fix đã triage vòng 1 nhưng nhánh chưa merge (không có file test fix nào của nó trên main). (1) GoldenRunner: run() dùng items = raw.get('data', []) nên với data/golden/tiktok/video_list.json dạng {'data': {'videos': [...]}} thì len(items)=3 (số key) và items[0] KeyError — golden TikTok không kiểm tra được schema thật. Fix: unwrap đệ quy — data là dict có 'videos' thì items=data['videos'], data là list thì giữ nguyên, data lồng nhau thì unwrap tiếp; test GoldenRunner với đúng data/golden/tiktok/video_list.json. (2) KillSwitch: ngưỡng mặc định RISK_STOP_THRESHOLD=0.9 quá muộn và không caller nào đọc .tripped (Monitor.record_job() chỉ ghi lại, scheduler không tham chiếu KillSwitch) nên là dead code. Fix: tách ngưỡng riêng KILL_SWITCH_ERROR_RATE_THRESHOLD=0.4 (cấu hình được 0.3-0.5); thêm kill_switch_gate()/Monitor.check_kill_switch() trả (False, lý do) khi tripped; bắt buộc TaskSchedulerImpl.enqueue() và acquire_job() gọi gate đó và REJECT job khi đã trip — phần nối scheduler hạ cánh SAU khi fix_scheduler_scheduled_at_lost merge xong để tránh xung đột (theo backend-sec), hoặc trong cùng cửa sổ scheduler với rebase; test trip rồi enqueue bị từ chối. (3) Prometheus: prometheus_text() in '# TYPE <tên> counter' trước mỗi dòng label. Fix: nhóm counter theo tên metric, in # TYPE một lần cho mỗi tên rồi mới in các dòng label; test mỗi tên metric chỉ có một dòng TYPE.

## Bài làm

Nhánh `fix/fix_monitor_golden_killswitch_prometheus` — chỉ nộp 2 file theo quy tắc nhánh `fix/*`. Đây là bản khôi phục nhánh fix đã nộp trước nhưng chưa merge (không có file test nào của nó trên `main`), rebase lên bản `main` hiện tại, với một sửa quan trọng: fixture TikTok dùng trong test giờ là **fixture thật trên `main`** — `data/golden/tiktok/video_list.json` có 1 video id `7512345678901234567`, stats lồng trong `"stats"` (`play_count` 1000, `digg_count` 120, `comment_count` 15, `share_count` 8, `collect_count` 20), không phải shape flat 2 video tự bịa ở lần nộp trước; adapter_map trong test đọc đúng `stats.*` và kỳ vọng đúng các số đó.

Những gì sửa trong `src/observability/monitor.py` (mọi phần khác giữ nguyên verbatim):
- **Golden unwrap**: thêm `unwrap_fixture_items()` đệ quy — list giữ nguyên; dict có `videos`/`items` dạng list thì lấy list đó; có `data` thì unwrap tiếp; còn lại `[]`. `GoldenRunner.run()` dùng nó thay cho `raw.get("data", [])` cũ (với fixture TikTok cũ cho `len(items)=3` là số key của dict và `items[0]` KeyError, golden không kiểm tra được schema thật).
- **Kill-switch**: tách ngưỡng riêng `KILL_SWITCH_ERROR_RATE_THRESHOLD = 0.4` (ghi chú khoảng cấu hình 0.3–0.5), bỏ hẳn việc dùng `RISK_STOP_THRESHOLD = 0.9` làm mặc định; `KillSwitch` mặc định 0.4. Thêm `kill_switch_gate(kill_switch)` và `Monitor.check_kill_switch()` trả `(False, lý_do)` khi đã trip, `Monitor(kill_switch=...)` / `kill_switch_threshold=` để inject, và property `Monitor.error_rate`. Scheduler vẫn chưa nối trong nhánh này (đúng phương án hội đồng: nối ở `TaskSchedulerImpl.enqueue()`/`acquire_job()` sau khi `fix_scheduler_scheduled_at_lost` merge xong để tránh xung đột) — test ghi rõ điểm nối đó, và gate đã sẵn sàng để scheduler gọi.
- **Prometheus**: `prometheus_text()` nhóm counter theo tên metric, in `# TYPE <tên> counter` một lần cho mỗi tên rồi mới in các dòng label; gauge giữ nguyên.

Kiểm thử trên VM Linux (Python 3.12.3, pytest 9.2.0) với fake adapter/fixture: `pytest tests/test_fix_monitor_golden_killswitch_prometheus.py` = **10 passed**, gồm unwrap đủ shape, GoldenRunner chạy đúng fixture thật trên `main` (1 video, `play_count` 1000) và fail đúng khi count kỳ vọng sai, kill-switch trip ở 40% lỗi và không trip dưới ngưỡng, gate từ chối khi đã trip và cho qua khi chưa trip, `Monitor.record_job()` nuôi kill-switch, và mỗi tên metric chỉ có một dòng `# TYPE`. Kết quả thật trên `main` do CI báo cáo.

(đã nộp file `src/observability/monitor.py`)

(đã nộp file `tests/test_fix_monitor_golden_killswitch_prometheus.py`)

## File đã nộp

- `src/observability/monitor.py`
- `tests/test_fix_monitor_golden_killswitch_prometheus.py`
