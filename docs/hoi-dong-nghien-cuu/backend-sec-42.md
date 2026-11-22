# Nhánh `expert/backend-sec-m2` (5 hướng giải quyết)

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

- [approach_03] Vault mã hóa credential, cookie và quản lý vòng đời token (`src/accounts/vault.py`): Tách secrets khỏi DB nghiệp vụ: mật khẩu, cookie, access_token, refresh_token, TOTP seed dùng envelope encryption AES-256-GCM, mỗi account một DEK riêng bọc bởi KEK trong KMS/Vault, chỉ giải mã trong bộ nhớ tại worker và tự xóa sau job. Token manager tự refresh trước hạn 15-30 phút với distributed lock theo account_id để tránh race refresh, phát hiện token bị thu hồi/checkpoint và chuyển account sang NEEDS_REAUTH thay vì retry vô hạn. Hỗ trợ OAuth2 (Facebook/TikTok/YouTube) và nhập cookie/session thủ công có kiểm tra hợp lệ. Tuyệt đối không log secret dạng plaintext, không trả secret ra API, mask khi hiển thị.
- [approach_07] Connector YouTube: Data API v3 + Analytics API (`src/connectors/youtube_connector.py`): Dùng YouTube Data API v3 lấy channel, playlist, danh sách video (views, likes, comments, publishedAt, duration) và YouTube Analytics API lấy watch time, impressions, CTR, subscriber gained/lost theo ngày. Quản lý quota unit chặt chẽ: cache, gom batch request, lập lịch ưu tiên kênh/video mới hoặc đang tăng trưởng. Hỗ trợ nhiều channel trên cùng một Google account và nhiều Google account độc lập.
- [approach_11] Cô lập browser profile và mô phỏng hành vi chống phát hiện (`src/browser/profile_manager.py`): Mỗi account có một browser profile riêng biệt (Playwright/Chromium persistent context): thư mục user-data riêng, cookie jar riêng, user-agent, timezone, locale, độ phân giải, WebGL/Canvas fingerprint ổn định và khớp geo của proxy, không bao giờ dùng chung profile giữa các account. Worker chạy trong container/sandbox riêng theo job, giới hạn CPU/RAM/timeout, tự kill và dọn profile tạm sau khi xong để chống Chromium zombie. Mô phỏng hành vi người dùng: thời gian xem theo phân phối log-normal theo độ dài video, khoảng nghỉ theo Poisson, thao tác cuộn/like/comment xác suất thấp, thêm jitter cho mọi khoảng thời gian, tránh mẫu máy móc. Phát hiện checkpoint/captcha thì dừng job, chụp screenshot khi lỗi và chuyển sang hàng đợi xử lý thủ công. Hỗ trợ mở profile headful qua noVNC/streaming từ UI để người dùng xử lý captcha rồi tự lưu lại cookie/session vào Vault.
- [approach_15] Phân nhóm, tagging và rule engine cảnh báo (`src/api/alert_rule_engine.py`): Tổ chức account theo Group (chiến dịch/kênh/chủ sở hữu) và Tag tự do, gán hàng loạt. Rule engine cảnh báo: account checkpoint/banned, proxy chết, token hết hạn trong 24h, video views tăng đột biến hoặc tụt bất thường, video bị ẩn/xóa (so sánh list video kỳ trước), sync thất bại liên tiếp. Cảnh báo hiển thị trong notification center và đẩy ra Telegram/Webhook/Discord theo cấu hình. Đây là phần biến công cụ quản lý thô thành hệ thống vận hành hoàn thiện.
- [approach_19] Thu thập và xây dựng bộ dữ liệu thực tế, fixtures và benchmark trên máy ảo Linux (`scripts/collect_sample_data.py`): Chạy trên máy ảo Linux (8GB RAM / 150GB Disk) để thu thập và xây dựng bộ dữ liệu thực tế phục vụ phát triển và kiểm thử: fixtures mẫu cho mỗi adapter (Facebook Graph API, TikTok API, YouTube Data API v3) gồm response thô, video list và metrics snapshot; bộ golden cases cố định cho CI phát hiện khi API đổi schema; dataset benchmark đo latency, success-rate theo proxy và theo platform; dữ liệu mẫu anonymized cho dashboard demo. Script thu thập có kiểm soát rate-limit, không lưu credential thật, tách dữ liệu thô và dữ liệu đã chuẩn hóa, kèm README mô tả nguồn, giấy phép và cách tái tạo.

## Bài làm

Đã kiểm thử trên máy ảo Linux: cài `cryptography 50.0.2`, `py_compile` cả 5 file đạt, smoke test end-to-end đạt — Vault store/rotate/token-refresh bị thu hồi → NEEDS_REAUTH, YouTube list_videos/get_video_stats/analytics (watch_time 600s, followers_delta=2, ctr=0.025, quota used=9), Browser fingerprint ổn định + checkpoint detection, Alert group/spike/sync-fail-3-lần, collect_sample_data sinh golden cases + benchmark.csv. Lưu ý bản stub `src/contracts.py` chỉ dùng cục bộ để test, không nộp — bản nộp import đúng contract trên main.

(đã nộp file `src/accounts/vault.py`)

(đã nộp file `src/connectors/youtube_connector.py`)

(đã nộp file `src/browser/profile_manager.py`)

(đã nộp file `src/api/alert_rule_engine.py`)

(đã nộp file `scripts/collect_sample_data.py`)

## File đã nộp

- `src/accounts/vault.py`
- `src/connectors/youtube_connector.py`
- `src/browser/profile_manager.py`
- `src/api/alert_rule_engine.py`
- `scripts/collect_sample_data.py`
