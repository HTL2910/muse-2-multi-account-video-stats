# Sửa lỗi `fix/fix_frontend_backend_contract_mismatch`: Chốt contract frontend-backend duy nhất từ OpenAPI

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_frontend_backend_contract_mismatch (fullstack-ux). Frontend gọi fetch('/api/accounts?...'), '/api/videos', '/api/jobs', '/api/proxies' trong khi FastAPI đăng ký route không tiền tố nên 404; list_accounts bắt buộc workspace_id mà frontend không gửi nên 422; backend AccountOut chỉ có {id, workspace_id, platform, platform_user_id, display_name, status, health_score, proxy_binding_id, last_sync_at} nhưng frontend đọc a.tags.join(...), a.avatar_url và a.proxy.{ip,country,latency_ms} nên tags undefined sẽ TypeError; nút Retry gọi POST /jobs/{id}/retry, Overview gọi /api/analytics/overview, Export gọi /api/export/videos.xlsx đều không tồn tại; frontend mở EventSource('/api/events') SSE trong khi backend chỉ có WebSocket '/ws/events'; bulk assign-proxy POST không body trong khi backend đòi body.proxy_id. Fix: chốt một contract duy nhất — backend mount mọi route dưới /api/v1 (hoặc frontend bỏ /api); sinh TypeScript client từ OpenAPI thay vì viết tay; mở rộng AccountOut (hoặc AccountRowOut) gồm tags, group_ids, avatar_url và object proxy đã join từ ProxyBinding+ProxyInfo (chỉ ip/country/latency/status, không credential); bổ sung /analytics/overview, /jobs/{id}/retry (gọi TaskScheduler.requeue_dead_letter), /export/* có audit; thống nhất một giao thức realtime — thêm SSE endpoint /events phát cùng EventHub hoặc đổi frontend sang WebSocket client; assign-proxy từ UI phải gửi {proxy_id} hoặc để backend tự acquire qua ProxyPool.

## Bài làm

Đã kiểm thử trên máy ảo: file .tsx mới chạy `npm run typecheck` (script từ package.json đã merge) cho exit 0, và bộ test contract tĩnh PASS. Quyết định chốt contract (ghi rõ trong header của file): nguồn sự thật duy nhất là OpenAPI của backend hiện hành — vì nhánh này chỉ được nộp file frontend, frontend được căn lại theo backend thay vì chờ backend đổi: bỏ toàn bộ tiền tố `/api`, gửi `workspace_id` bắt buộc (lấy từ URL) và lược bỏ filter rỗng, các interface `AccountRow/VideoRow/JobRow/ProxyRow` khớp đúng schema backend (bỏ `tags`/`avatar_url`/`group_ids`/proxy object/thumbnail — hết lỗi `undefined.join`), metrics lấy từ `/videos/{id}/metrics` dạng Page, Overview tự tổng hợp từ các list endpoint có thật thay cho `/analytics/overview`, bỏ nút Retry/Export Excel/Gán tag (endpoint không tồn tại), realtime chuyển từ EventSource sang WebSocket `/ws/events`, và bulk assign-proxy nay gửi body `{proxy_id}` của proxy ACTIVE đầu tiên như backend yêu cầu. Nếu hội đồng muốn chiều ngược lại (backend mount `/api/v1` + `AccountRowOut` mở rộng), đó là việc của nhánh api riêng — contract trong file này vẫn là điểm đối chiếu để sinh client từ OpenAPI.

(đã nộp file `src/web/pages/DashboardAccounts.tsx`)

(đã nộp file `tests/test_fix_frontend_backend_contract_mismatch.py`)

## File đã nộp

- `src/web/pages/DashboardAccounts.tsx`
- `tests/test_fix_frontend_backend_contract_mismatch.py`
