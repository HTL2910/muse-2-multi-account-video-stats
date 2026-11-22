# Sửa lỗi `fix/fix_tiktok_upload_scope`: Xóa scope video.upload khỏi TikTok REQUIRED_SCOPES

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_tiktok_upload_scope (muse) + phần scope của fix_tiktok_excess_scope_and_test_portabi (fullstack-ux). Hệ thống chỉ đọc thống kê, scope video.upload cho phép đăng video thay người dùng, vi phạm least privilege và khó duyệt OAuth. Fix: xóa 'video.upload' khỏi REQUIRED_SCOPES, chỉ giữ user.info.basic, video.list và scope đọc insights cần thiết; ghi rõ trong docstring: chỉ đọc, không đăng/like/comment.

## Bài làm

Đã kiểm thử trên máy ảo (stub contracts v1.0.0, fake HTTP): cả 5 test PASS — `REQUIRED_SCOPES` đúng bằng `["user.info.basic", "video.list"]`, không scope ghi nào lọt vào REQUIRED + OPTIONAL, `authenticate()` cấp TokenSet chỉ mang scope đọc, và các đường đọc (`list_videos`, `get_video_stats`) vẫn chạy đúng sau khi bỏ scope. Ngoài việc xóa scope, docstring module ghi rõ hợp đồng READ-ONLY (chỉ đọc thống kê, không đăng/like/comment thay người dùng) và có guard rail dạng assert lúc import để scope ghi không bao giờ quay lại danh sách xin quyền. Chỉ nộp file mục tiêu + file test riêng.

(đã nộp file `src/connectors/tiktok_connector.py`)

(đã nộp file `tests/test_fix_tiktok_upload_scope.py`)

## File đã nộp

- `src/connectors/tiktok_connector.py`
- `tests/test_fix_tiktok_upload_scope.py`
