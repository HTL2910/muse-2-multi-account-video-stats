# Nhánh `expert/fullstack-ux-m2` (5 hướng giải quyết)

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

- [approach_04] Proxy pool manager: sticky 1-1, chấm điểm, health-check và failover (`src/proxy/proxy_pool_manager.py`): Quản lý pool proxy HTTP/HTTPS/SOCKS5 residential/datacenter/mobile theo quốc gia/ISP, import hàng loạt format host:port:user:pass, credential proxy cũng mã hóa. Mỗi account bind sticky 1 proxy cố định (1 account = 1 proxy + 1 fingerprint) để tránh nhảy IP gây checkpoint; chỉ rotate khi proxy chết hoặc theo lịch, chỉ chuyển sang proxy cùng geo và đưa account về warm-up. Chấm điểm động: score = w1*success_rate + w2*(1/latency_norm) + w3*stability - w4*ban_rate - w5*cost, cập nhật EWMA sau mỗi request, kết hợp consistent hashing account_id -> proxy và epsilon-greedy thử proxy mới. Health-check định kỳ latency, success-rate, IP leak/DNS leak, blacklist; proxy lỗi 3 lần liên tiếp bị quarantine, circuit-breaker + backoff hàm mũ, failover sang dự phòng cùng geo. Cấp phát bằng lease có TTL + heartbeat, ghi lịch sử account-proxy-IP để truy vết.
- [approach_08] Pipeline ingestion chuẩn hóa đa nền tảng (Adapter + ETL + Validation) (`src/ingestion/pipeline.py`): Pipeline 4 bước: Discover (quét video mới) -> Fetch (gọi connector qua đúng proxy của account) -> Normalize (map về UnifiedVideoMetric: video_id, platform, account_id, views, likes, comments, shares, watch_time, followers_delta, collected_at) -> Store (snapshot theo ngày, không ghi đè). Validate schema, loại giá trị âm, tăng đột biến bất hợp lý, thiếu trường bắt buộc thì đánh dấu partial/NEEDS_REVIEW thay vì điền 0 hoặc suy đoán. Deduplicate theo (account_id, platform_video_id, date bucket) với idempotency-key, xử lý video bị xóa/private, chuẩn hóa đơn vị và múi giờ UTC.
- [approach_12] Dashboard UX tổng quan đa tài khoản và chi tiết video (`src/web/pages/DashboardAccounts.tsx`): SPA React/Next.js + TypeScript gồm 4 màn hình: 1) Account Grid/Table lọc theo platform/status/proxy/group/tag, tìm kiếm, bulk action (check alive, sync, gán proxy, gán tag), trạng thái sống/chết bằng màu sắc, badge IP/quốc gia/latency; 2) Dashboard tổng: KPI cards tổng views/likes/comments, biểu đồ time-series so sánh 3 nền tảng, top video tăng trưởng nhanh; 3) Video Explorer: bảng video sort theo views/velocity, click xem biểu đồ lịch sử metrics từng video và link gốc, thumbnail; 4) Proxy Manager + Settings + Job Monitor xem job fail và retry thủ công/hàng loạt. Dùng TanStack Query + WebSocket/SSE cập nhật realtime, virtual scroll cho nghìn account/video, dark mode, export CSV/Excel, mọi bulk action có confirm + progress bar + kết quả từng item.
- [approach_16] RBAC, xác thực người vận hành và audit trail bất biến (`src/security/rbac_audit.py`): API quản trị có xác thực mạnh (argon2id/bcrypt cho mật khẩu người dùng hệ thống, bắt buộc 2FA TOTP cho admin), RBAC theo vai trò Admin/Manager/Operator/Viewer theo workspace, mỗi tenant chỉ thấy account của mình (Row-Level Security / tenant_id). Mọi thao tác nhạy cảm (xem cookie, đổi proxy, xóa account, export dữ liệu, thêm/xóa account) ghi audit_log append-only có hash-chain, không sửa/xóa được. Rate-limit và khóa brute-force ở gateway, CORS/CSRF đúng chuẩn, header bảo mật, session người dùng có timeout và thu hồi được.
- [approach_20] Đóng gói triển khai, sao lưu/khôi phục và tuân thủ ToS (`docker/docker-compose.yml`): Đóng gói Docker Compose gồm api, worker, scheduler, postgres, redis, frontend để triển khai 1 lệnh; worker stateless scale ngang bằng Docker/K8s, graceful shutdown, job lease/timeout chống treo. Backup DB và vault keys tách riêng, mã hóa khi lưu trữ, keys không bao giờ nằm cùng nơi với bản backup dữ liệu, kiểm thử khôi phục định kỳ và disaster recovery. Tuân thủ ToS: chỉ dùng API chính thức khi có thể, tôn trọng giới hạn nền tảng, không tự động tương tác like/comment hàng loạt, tôn trọng quyền riêng tư và chính sách xóa dữ liệu theo yêu cầu.

## Bài làm

Đã triển khai và kiểm thử trên máy ảo Linux: `python3 tests/test_fullstack_ux_m2.py` → ALL TESTS PASSED (proxy sticky/quarantine/failover fail-closed, pipeline dedupe/negative/spike/partial, RBAC+RLS, password hash, TOTP RFC6238, audit hash-chain tamper detection, session revoke, brute-force lockout); `docker-compose.yml` parse YAML OK. Cả 3 module Python chỉ dùng stdlib + `src/contracts.py` (không đổi tên Enum/dataclass/chữ ký nào), TSX khớp type của contracts.

(đã nộp file `src/proxy/proxy_pool_manager.py`)

(đã nộp file `src/ingestion/pipeline.py`)

(đã nộp file `src/web/pages/DashboardAccounts.tsx`)

(đã nộp file `src/security/rbac_audit.py`)

(đã nộp file `docker/docker-compose.yml`)

(đã nộp file `tests/test_fullstack_ux_m2.py`)

## File đã nộp

- `src/proxy/proxy_pool_manager.py`
- `src/ingestion/pipeline.py`
- `src/web/pages/DashboardAccounts.tsx`
- `src/security/rbac_audit.py`
- `docker/docker-compose.yml`
- `tests/test_fullstack_ux_m2.py`
