# Nhánh `expert/systems-arch-m2` (5 hướng giải quyết)

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

- [approach_01] Kiến trúc core đa tài khoản và PlatformAdapter thống nhất (`src/core/models.py`): Thiết kế Modular Monolith với các module độc lập: Account, Proxy, Collector, Stats, Dashboard API, giao tiếp qua API Gateway + message queue. Định nghĩa interface chung PlatformAdapter (authenticate, check_alive, list_videos, get_video_stats, refresh_token) và 3 adapter con cho Facebook/TikTok/YouTube. Định nghĩa entity trung tâm: Workspace, PlatformAccount, Video, VideoMetricDaily, Proxy, Task. Core chỉ làm việc với schema chung, thêm nền tảng mới chỉ cần viết adapter mới. Ban đầu triển khai monolith, tách service khi vượt ~500 account hoặc >10k video/ngày.
- [approach_05] Connector Facebook: Graph API + Page/Video Insights (`src/connectors/facebook_connector.py`): Dùng Facebook Graph API / Instagram Graph API qua Meta for Developers: lấy Page, video/Reels, insights (views, reach, likes, comments, shares, watch time, followers). Xử lý phân trang, token Page dài hạn, throttle + exponential backoff khi rate-limit. Map video_id gốc sang video chuẩn nội bộ, ghi rõ nguồn dữ liệu cho mỗi bản ghi. Fallback có kiểm soát bằng đọc dữ liệu công khai qua browser profile của account khi API thiếu field, có giới hạn tần suất và log nguồn.
- [approach_09] Scheduler và hàng đợi tác vụ phân tán dưới ràng buộc quota (`src/scheduler/task_scheduler.py`): Mọi thao tác thu thập là Job trong priority queue (Celery/RQ + Redis hoặc BullMQ) với queue riêng theo platform và loại việc: login/check-live, fetch-stats, refresh-token. Scheduler (Celery Beat/Temporal) lập lịch theo tần suất: video mới 15 phút, video cũ 6-12 giờ, account VIP ưu tiên. Ràng buộc: per-account concurrency = 1, per-proxy concurrency giới hạn, per-platform quota/ngày bằng token bucket + leaky bucket + semaphore phân tán, jitter ngẫu nhiên. Shard worker theo account_id (consistent hashing) để cùng account luôn về cùng worker, distributed lock Redis Redlock chống chạy trùng, job có lease/visibility timeout, heartbeat, retry exponential backoff tối đa 3 lần rồi vào dead-letter queue. Ưu tiên theo độ tươi dữ liệu, deadline và chi phí quota bằng weighted fair scheduling.
- [approach_13] REST API + WebSocket Gateway làm lớp tích hợp end-to-end (`src/api/main.py`): Backend FastAPI (hoặc NestJS) phân lớp Routers -> Service -> Adapter/Worker. API Gateway thống nhất REST/GraphQL, xác thực JWT, rate-limit theo tenant. Cung cấp REST chuẩn: /auth, /accounts (CRUD + import hàng loạt), /accounts/{id}/check-alive, /accounts/{id}/sync-videos, /accounts/{id}/assign-proxy, /videos, /videos/{id}/metrics?from&to, /proxies, /proxies/health, /proxies/import, /groups, /tags, /jobs. Phân trang cursor-based, filter platform/status/search thống nhất. WebSocket/SSE channel /ws/events đẩy sự kiện account_status_changed, sync_completed, alert. Sinh OpenAPI/Swagger tự động và SDK client TypeScript từ OpenAPI để frontend không gọi API thủ công.
- [approach_17] Observability, cổng fail-closed và kiểm thử golden (`src/observability/monitor.py`): Lớp observability độc lập với pipeline chính: Prometheus metrics, OpenTelemetry trace theo account_id/job_id, structured logging đã redact secret/PII, log tập trung, alerting qua Telegram/Email khi tỉ lệ lỗi tăng đột biến. Mỗi batch thu thập phải qua kiểm tra P0: schema hợp lệ, views không giảm bất thường, số lượng video khớp danh sách kỳ vọng, proxy và account đều ACTIVE; nếu thất bại thì batch bị REJECTED/NEEDS_REVIEW, không ghi đè dữ liệu cũ. Hệ thống fail-closed: proxy chết, session hết hạn hoặc API trả lỗi xác thực thì job bị REJECTED và ghi audit, không dùng proxy/session mặc định của account khác. Có bộ golden test với dữ liệu mẫu cố định cho mỗi adapter để CI phát hiện ngay khi API nền tảng đổi schema, và kill-switch dừng toàn bộ crawl khi tỉ lệ lỗi vượt ngưỡng.

## Bài làm

systems-arch — expert/systems-arch-m2 — đã hoàn thành 5 hướng: approach_01, 05, 09, 13, 17.

Kiểm thử trên máy ảo Linux: Python 3.12.3, venv với FastAPI 0.165.0, pytest 9.2.0 — `python -m pytest tests -q` = 15 passed. Lưu ý trung thực: test chạy với stub cục bộ của `src/contracts.py` v1.0.0 (các field/chữ ký dùng trong 5 file này khớp bản trên main đã đọc qua XEM_FILE), không phải với repo main thật và không gọi Graph API thật (không có credential thật, `http_get` được inject). CI trên main phải chạy lại. InMemory store/scheduler là bản test/dev, production cần PostgreSQL/Redis/Celery; `/auth/login` trả 501, chờ approach_16 (RBAC).

(đã nộp file `src/core/models.py`)

(đã nộp file `src/connectors/facebook_connector.py`)

(đã nộp file `src/scheduler/task_scheduler.py`)

(đã nộp file `src/api/main.py`)

(đã nộp file `src/observability/monitor.py`)

(đã nộp file `tests/test_systems_arch_m2.py`)

(đã nộp file `data/golden/facebook/video_list.json`)

## File đã nộp

- `src/core/models.py`
- `src/connectors/facebook_connector.py`
- `src/scheduler/task_scheduler.py`
- `src/api/main.py`
- `src/observability/monitor.py`
- `tests/test_systems_arch_m2.py`
- `data/golden/facebook/video_list.json`
