# Nhánh `expert/algo-ml-m2` (5 hướng giải quyết)

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

- [approach_02] Mô hình tài khoản thống nhất và máy trạng thái vòng đời (`src/accounts/account_model.py`): Một Account entity duy nhất cho 3 nền tảng: platform, platform_user_id, owner, niche, status, proxy_binding_id, profile_id, health_score, last_check_at. Máy trạng thái hữu hạn: CREATED -> VERIFIED -> ACTIVE -> TOKEN_EXPIRING -> REFRESHING -> ACTIVE/NEEDS_REVIEW, và các trạng thái lỗi CHECKPOINT, CAPTCHA_REQUIRED, TOKEN_EXPIRED, RATE_LIMITED, BANNED, DISABLED. Mọi chuyển trạng thái ghi audit log bất biến, không cho trạng thái mơ hồ. Hỗ trợ import/export hàng loạt CSV/Excel, gắn tag, nhóm, owner. PostgreSQL là source of truth, upsert idempotent theo khóa tự nhiên từng nền tảng, tách raw_payload JSONB có TTL khỏi dữ liệu đã chuẩn hóa.
- [approach_06] Connector TikTok: API chính thức + thu thập bổ sung có kiểm soát (`src/connectors/tiktok_connector.py`): Ưu tiên TikTok Research API / Display API / Business API / Login Kit để lấy profile, danh sách video, views, likes, comments, shares, saves và follower count. Xử lý OAuth scope, refresh token và quota theo app. Với chỉ số không có qua API, dùng module thu thập bổ sung qua web với proxy + browser profile riêng của account, giới hạn concurrency và jitter, không spam request. Luôn ghi rõ dữ liệu nào từ API, dữ liệu nào từ thu thập bổ sung.
- [approach_10] Kho dữ liệu time-series và phân tích tăng trưởng video (`src/analytics/time_series.py`): PostgreSQL lưu dữ liệu quan hệ (account, video, user); TimescaleDB/ClickHouse lưu time-series video_metrics (time, video_id, views, likes, comments, shares) phân vùng theo platform + tháng, nén và retention tự động archive sang object storage. Tính chỉ số phái sinh bằng cửa sổ trượt: growth_rate 1h/24h, velocity views/giờ, engagement_rate = (likes+comments+shares)/views, retention proxy, top video. Pre-aggregation rollup 1h/1d, materialized view và cache Redis với TTL cho dashboard. Phát hiện bất thường bằng z-score trên chuỗi đã khử xu hướng và Isolation Forest đa chiều để cảnh báo video tăng đột biến, tụt view hoặc dữ liệu lệch do API lỗi.
- [approach_14] Account health score, dự đoán rủi ro khóa và xử lý checkpoint fail-closed (`src/risk/account_health.py`): Tính Account Health Score từ tỉ lệ sync thành công, số checkpoint, captcha, rate-limit 429, proxy lỗi. Xây feature vector (tần suất request, tỷ lệ lỗi 4xx/429, số lần đổi proxy/IP, độ lệch hành vi so với baseline, tuổi tài khoản, lịch sử checkpoint) và mô hình phân loại Logistic Regression/Gradient Boosting cho risk_score 0-1 và nhãn RISK_LOW/MEDIUM/HIGH, retrain định kỳ từ dữ liệu checkpoint thực tế kèm giải thích feature quan trọng nhất. Confidence gate fail-closed: risk > 0.7 tự giảm tần suất 50%, đổi proxy sạch, tạm dừng tác vụ không thiết yếu; risk > 0.9 dừng hẳn và đưa vào NEEDS_REVIEW. Bất kỳ tín hiệu bất thường nào (login lạ, checkpoint, 2FA, đổi mật khẩu, API trả lỗi quyền) đều chuyển trạng thái ngay, dừng mọi job tự động của account đó, không thử đăng nhập lặp lại, đưa vào hàng đợi xử lý thủ công kèm bằng chứng (mã lỗi, thời điểm, proxy, screenshot/log đã che secret).
- [approach_18] Suy luận thông minh: gợi ý nội dung, dự báo views và phân cụm video (`src/ai/recommendation_engine.py`): Dùng dữ liệu chỉ số đã chuẩn hóa để suy luận: phân cụm video bằng TF-IDF + embedding tiêu đề/mô tả và K-Means/HDBSCAN để tìm chủ đề hiệu quả theo từng tài khoản; dự báo views 24h/7 ngày bằng mô hình chuỗi thời gian (Prophet/ARIMA hoặc LightGBM với feature thời gian + lịch sử); tối ưu thời điểm đăng bằng bài toán tìm cực đại của hàm engagement theo giờ/ngày trong tuần từ dữ liệu lịch sử. Hệ gợi ý xếp hạng video nên xem/phân tích tiếp theo bằng content-based filtering kết hợp collaborative filtering giữa các tài khoản cùng niche. Kết quả chỉ là gợi ý có độ tin cậy kèm khoảng tin cậy, không tự động đăng khi chưa được duyệt.

## Bài làm

Nộp bài nhánh `expert/algo-ml-m2` — algo-ml, 5 hướng approach_02 / 06 / 10 / 14 / 18, tuân thủ `src/contracts.py` v1.0.0 (chỉ dùng Enum/dataclass/hàm đã khóa, không đổi tên, không log secret, fail-closed).

Đã chạy thử trên máy ảo Linux (Python 3.12, venv pytest 9.2.0): **17 passed** trong `tests/test_algo_ml_m2.py`. Ba lỗi phát hiện khi chạy thử đã sửa trong code (không sửa test để che lỗi): `check_token_lifecycle` phải cho phép TOKEN_EXPIRING -> TOKEN_EXPIRED; `compute_derived._nearest` phải loại điểm hiện tại khỏi ứng viên (không thì velocity luôn 0); `detect_anomalies` cần thêm quy tắc tăng >200% vì một cú nhảy đơn lẻ làm phình mean/sd của z-score. Tất cả module chỉ dùng thư viện chuẩn Python (logistic regression, Isolation Forest, TF-IDF + K-Means, forecast log-trend đều tự cài đặt, không cần sklearn/Prophet), nên chạy được trong CI.

Lưu ý: `src/contracts.py` đã có trên `main` nên không nộp lại; file stub dùng để test cục bộ không nộp.

(đã nộp file `src/accounts/account_model.py`)

(đã nộp file `src/connectors/tiktok_connector.py`)

(đã nộp file `src/analytics/time_series.py`)

(đã nộp file `src/risk/account_health.py`)

(đã nộp file `src/ai/recommendation_engine.py`)

(đã nộp file `tests/test_algo_ml_m2.py`)

(đã nộp file `data/golden/tiktok/video_list.json`)

## File đã nộp

- `src/accounts/account_model.py`
- `src/connectors/tiktok_connector.py`
- `src/analytics/time_series.py`
- `src/risk/account_health.py`
- `src/ai/recommendation_engine.py`
- `tests/test_algo_ml_m2.py`
- `data/golden/tiktok/video_list.json`
