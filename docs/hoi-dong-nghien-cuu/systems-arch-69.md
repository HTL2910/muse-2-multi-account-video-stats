# Sửa lỗi `fix/fix_monitor_golden_killswitch_prometheus`: GoldenRunner unwrap đúng shape, kill-switch nối vào scheduler, Prometheus TYPE một lần

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 2_

## Việc được giao

Gộp fix_monitor_golden_runner_fixture_shape + fix_monitor_killswitch_dead_and_threshol (algo-ml) + fix_monitor_prometheus_duplicate_type (systems-arch, đã kiểm chứng VM). GoldenRunner.run() làm items=raw.get('data',[]) nên với fixture TikTok data là dict {'videos':[...]} thì len(items)=3 (số key) và items[0] KeyError; KillSwitch ngưỡng mặc định 0.9 (RISK_STOP_THRESHOLD) quá muộn và không nơi nào kiểm tra tripped trước enqueue/acquire nên là dead code; prometheus_text() in '# TYPE jobs_total counter' trước mỗi dòng label khác nhau, sai chuẩn exposition. Fix: unwrap đệ quy — data là dict có 'videos' thì items=data['videos'], data là list thì giữ nguyên; tách ngưỡng kill-switch riêng cấu hình được (ví dụ 0.3-0.5), bắt buộc TaskSchedulerImpl.enqueue()/acquire_job() kiểm tra tripped và REJECT job khi đã trip; nhóm counter theo tên metric, in # TYPE một lần cho mỗi tên rồi mới in các dòng label. Test GoldenRunner với đúng data/golden/tiktok/video_list.json, test trip rồi enqueue bị từ chối, test mỗi tên metric chỉ có một dòng TYPE.

## Bài làm

Nhánh `fix/fix_monitor_golden_killswitch_prometheus` — sửa lại sau khi CI fail, chỉ nộp 2 file theo quy tắc nhánh `fix/*`.

**Nguyên nhân CI fail:** `GoldenCase` thật trên `main` có 2 field bắt buộc `id` và `name` mà stub contracts tại chỗ của tôi thiếu, nên test cũ gọi `GoldenCase(platform=..., raw_fixture_path=..., ...)` bị `TypeError: GoldenCase.__init__() missing 2 required positional arguments: 'id' and 'name'`. Lỗi chỉ ở **test**, code `monitor.py` không đổi.

**Đã sửa:**
- Test: cả 2 chỗ tạo `GoldenCase` giờ truyền `id="golden-tiktok-1", name="tiktok video list"`; stub contracts tại chỗ cũng được bổ sung 2 field bắt buộc đó để lần sau bắt được lỗi này ngay trên VM.
- `src/observability/monitor.py` giữ nguyên bản đã nộp trước (unwrap đệ quy `unwrap_fixture_items()`, ngưỡng kill-switch riêng `0.4` + `kill_switch_gate()`/`Monitor.check_kill_switch()`, Prometheus `# TYPE` một lần cho mỗi tên metric).

Kiểm thử lại trên VM Linux (Python 3.12.3, pytest 9.2.0): `pytest tests/test_fix_monitor_golden_killswitch_prometheus.py` = **10 passed**. Kết quả thật trên `main` do CI báo cáo.

(đã nộp file `src/observability/monitor.py`)

(đã nộp file `tests/test_fix_monitor_golden_killswitch_prometheus.py`)

## File đã nộp

- `src/observability/monitor.py`
- `tests/test_fix_monitor_golden_killswitch_prometheus.py`
