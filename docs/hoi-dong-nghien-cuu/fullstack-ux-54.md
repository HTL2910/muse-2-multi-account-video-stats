# Sửa lỗi `fix/fix_golden_missing_youtube`: Thống nhất một cây golden data/golden đủ 3 nền tảng

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_golden_missing_youtube_duplicate (muse) và phần golden của fix_golden_youtube_missing_and_registry_. Chọn một nguồn duy nhất data/golden/ theo cấu trúc đã khóa: bổ sung data/golden/youtube/video_list.json và data/golden/cases.json; xóa hoặc biến data/sample/golden thành tài liệu trỏ về data/golden. Sửa build_golden_cases() trong scripts/collect_sample_data.py để raw_fixture_path trỏ đúng file thật cho cả 3 platform. Test: assert đủ 3 file golden tồn tại và mọi GoldenCase.raw_fixture_path đều tồn tại trên repo.

## Bài làm

Đã kiểm thử trên máy ảo: test PASS với cây golden đủ 3 nền tảng, và negative control (xóa file youtube → test fail đúng assertion “missing canonical golden fixture”, khôi phục → PASS lại). Hai điểm đã kiểm chứng trước khi nộp: (1) `build_golden_cases()` trong `scripts/collect_sample_data.py` trên main vốn đã trỏ đúng `data/golden/{platform}/video_list.json` cho cả 3 platform nên không cần sửa script; (2) nội dung file YouTube mới giống hệt fixture ở `data/sample/golden/youtube/` và khớp `expected_first_metric` của golden case (yt_video_001, 98000/6100/540) — tức `data/golden/` trở thành nguồn chuẩn duy nhất, cây sample chỉ còn là bản sao lịch sử. Theo quy tắc nhánh fix, chỉ nộp file mục tiêu + file test riêng (không nộp `data/golden/cases.json` hay file của đồng nghiệp).

(đã nộp file `data/golden/youtube/video_list.json`)

(đã nộp file `tests/test_fix_golden_missing_youtube.py`)

## File đã nộp

- `data/golden/youtube/video_list.json`
- `tests/test_fix_golden_missing_youtube.py`
