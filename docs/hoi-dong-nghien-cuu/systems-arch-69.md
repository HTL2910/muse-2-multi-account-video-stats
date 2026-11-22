# Sửa lỗi `fix/fix_monitor_golden_killswitch_prometheus`: GoldenRunner unwrap đúng shape, kill-switch nối vào scheduler, Prometheus TYPE một lần

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_monitor_golden_runner_fixture_shape + fix_monitor_killswitch_dead_and_threshol (algo-ml) + fix_monitor_prometheus_duplicate_type (systems-arch, đã kiểm chứng VM). GoldenRunner.run() làm items=raw.get('data',[]) nên với fixture TikTok data là dict {'videos':[...]} thì len(items)=3 (số key) và items[0] KeyError; KillSwitch ngưỡng mặc định 0.9 (RISK_STOP_THRESHOLD) quá muộn và không nơi nào kiểm tra tripped trước enqueue/acquire nên là dead code; prometheus_text() in '# TYPE jobs_total counter' trước mỗi dòng label khác nhau, sai chuẩn exposition. Fix: unwrap đệ quy — data là dict có 'videos' thì items=data['videos'], data là list thì giữ nguyên; tách ngưỡng kill-switch riêng cấu hình được (ví dụ 0.3-0.5), bắt buộc TaskSchedulerImpl.enqueue()/acquire_job() kiểm tra tripped và REJECT job khi đã trip; nhóm counter theo tên metric, in # TYPE một lần cho mỗi tên rồi mới in các dòng label. Test GoldenRunner với đúng data/golden/tiktok/video_list.json, test trip rồi enqueue bị từ chối, test mỗi tên metric chỉ có một dòng TYPE.

## Bài làm

Nhánh `fix/fix_monitor_golden_killswitch_prometheus` — chỉ nộp 2 file theo quy tắc nhánh `fix/*`.

Ba lỗi đã sửa trong `src/observability/monitor.py` (mọi phần khác giữ nguyên):
- **GoldenRunner unwrap đệ quy**: thêm `unwrap_fixture_items()` — `data` là list thì giữ nguyên, dict có `videos`/`items` thì lấy list đó, dict có `data` thì unwrap tiếp (xử lý đúng `{"data": {"videos": [...]}}` của TikTok). Code cũ `items=raw.get('data', [])` cho `len(items)=3` (số key) rồi `items[0]` KeyError.
- **Kill-switch**: tách ngưỡng riêng `KILL_SWITCH_ERROR_RATE_THRESHOLD = 0.4` (cấu hình được, 0.3–0.5), thay cho mặc định cũ 0.9 (`RISK_STOP_THRESHOLD`) quá muộn. Thêm `kill_switch_gate()` và `Monitor.check_kill_switch()` trả `(False, lý_do)` khi đã trip, `(True, '')` khi chưa; `Monitor` nhận thêm `kill_switch=` để inject instance chung và `kill_switch_threshold=`, thêm property `error_rate`. **Lưu ý phạm vi**: việc nối gate vào `TaskSchedulerImpl.enqueue()/acquire_job()` để REJECT job thuộc file scheduler, không nộp được trong nhánh này (chỉ được nộp monitor + test) — gate API đã có sẵn và đã test, nhánh scheduler phải gọi `check_kill_switch()`/`kill_switch_gate()` trước enqueue/acquire.
- **Prometheus**: `prometheus_text()` nhóm counter theo tên metric, in `# TYPE <tên> counter` một lần cho mỗi tên rồi mới in các dòng label; gauge giữ một TYPE cho mỗi tên (vốn đã duy nhất).

Kiểm thử trên VM Linux (Python 3.12.3, pytest 9.2.0): `pytest tests/test_fix_monitor_golden_killswitch_prometheus.py` = **10 passed**, gồm: unwrap đủ 5 shape fixture (list, `data` list, TikTok `data.videos`, `videos` top-level, `data` lồng nhau), GoldenRunner với đúng shape `data/golden/tiktok/video_list.json` (2 video, metric đầu khớp) và test count sai fail đúng thông điệp, trip ở 50% lỗi / không trip ở 20% lỗi, gate từ chối khi tripped, `record_job` nuôi kill-switch rồi gate chặn, mỗi tên metric chỉ có một dòng TYPE và TYPE đứng trước các dòng dữ liệu của nó. Kết quả thật trên `main` do CI báo cáo.

(đã nộp file `src/observability/monitor.py`)

(đã nộp file `tests/test_fix_monitor_golden_killswitch_prometheus.py`)

## File đã nộp

- `src/observability/monitor.py`
- `tests/test_fix_monitor_golden_killswitch_prometheus.py`
