# Sửa lỗi `fix/fix_golden_unified_source`: Chốt data/golden làm nguồn golden duy nhất, sửa drift facebook

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_golden_cases_json_duplicate_tree (muse) + fix_golden_facebook_schema_drift (fullstack-ux) thành một lỗi gốc: hai cây golden song song đã lệch nhau thật. data/golden/ thiếu cases.json trong khi data/sample/golden/cases.json tồn tại; build_golden_cases() trong scripts/collect_sample_data.py trỏ raw_fixture_path tới data/golden/{platform}/video_list.json và kỳ vọng fixture do chính RAW_BUILDERS sinh (facebook: data[0].id='fb_video_001', insights lồng trong từng video, views=12000), nhưng data/golden/facebook/video_list.json trên main là fixture viết tay khác hẳn (id '987', views 100 trực tiếp, insights ở top-level) — fullstack-ux đã kiểm chứng normalize('facebook', fixture data/golden) cho views=0 thay vì 12000, trong khi data/golden/youtube/video_list.json khớp builder tuyệt đối và bản sao đúng của facebook nằm ở data/sample/golden/. Fix: chốt data/golden/ làm cây chuẩn (vì build_golden_cases trỏ vào đó) — sinh lại data/golden/* từ chính collect_sample_data (mở rộng main() để ghi thêm cây data/golden, hoặc copy từ data/sample/golden vốn do script sinh), đưa cases.json về data/golden/ với raw_fixture_path trỏ về data/golden/, data/sample/golden chỉ còn là output tái sinh hoặc tài liệu trỏ về; thêm test đối chiếu fixture-trên-đĩa == RAW_BUILDERS cho cả 3 platform (test này là phán quyết cuối cho drift: nếu fixture đĩa lệch builder thì sinh lại, không tranh luận thêm). Giữ test assert mọi GoldenCase.raw_fixture_path tồn tại và đủ 3 file golden.

## Bài làm

(đã nộp file `data/golden/cases.json`)

(đã nộp file `data/golden/facebook/video_list.json`)

(đã nộp file `data/golden/tiktok/video_list.json`)

(đã nộp file `data/golden/youtube/video_list.json`)

(đã nộp file `tests/test_fix_golden_unified_source.py`)

(đã nộp file `data/golden/cases.json`)

(đã nộp file `data/golden/facebook/video_list.json`)

(đã nộp file `data/golden/tiktok/video_list.json`)

(đã nộp file `data/golden/youtube/video_list.json`)

(đã nộp file `tests/test_fix_golden_unified_source.py`)

## File đã nộp

- `data/golden/cases.json`
- `data/golden/facebook/video_list.json`
- `data/golden/tiktok/video_list.json`
- `data/golden/youtube/video_list.json`
- `tests/test_fix_golden_unified_source.py`
