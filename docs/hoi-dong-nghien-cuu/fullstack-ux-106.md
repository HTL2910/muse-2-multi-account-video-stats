# Sửa lỗi `fix/fix_golden_path_a_script_alignment`: Sửa script sinh golden theo shape parser thật, dời mọi kỳ vọng về số chuẩn

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_golden_tree_duplication (systems-arch) + fix_golden_path_a_script_alignment (fullstack-ux) thành một lỗi gốc theo phán quyết của cả bốn chuyên gia phản biện: hai cây data/golden/ và data/sample/golden/ đang chứa hai sự thật khác nhau, và build_golden_cases() trong script vẫn sinh shape builder cũ (facebook fb_video_001/12000 với insights total_*, tiktok tt_video_001/50000 field phẳng) mà parser thật đã bác bỏ (shape phẳng parse thành PARTIAL/views=0 — algo-ml đã tự kiểm chứng), đồng thời trỏ raw_fixture_path vào chính data/golden — tức output script hiện MÂU THUẪN với data/golden/cases.json trên đĩa (facebook id 987/views 100/watch 1234; tiktok id 7512345678901234567/views 1000/likes 120/saves 20), và ai chạy lại script hôm nay sẽ tái sinh toàn bộ drift vừa dọn ('máy tái tạo drift'). Không xử lý riêng bằng cách xóa cây sample / redirect — cây sample là output mặc định của script, xóa mà không sửa script thì lần chạy lại tiếp theo tái sinh drift. Fix (đường A): giao chủ file script một nhánh riêng — facebook_raw() sinh đúng shape connector (VIDEO_FIELDS + insights top-level post_*), tiktok_raw() sinh đúng shape API thật (stats lồng, tên raw play_count/digg_count, cursor/has_more), normalize() đọc đúng các tên đó; sinh lại data/sample/*; trong CÙNG merge dời mọi kỳ vọng phía script còn trích số builder cũ (gồm assertion golden trong tests/test_backend_sec_m2.py) sang số chuẩn; khôi phục assert cases.json == build_golden_cases() cho cả 3 platform và thêm test đối chiếu hai cây không lệch schema/giá trị. Làm sau khi các mục đỏ CI (version, SQLite, dedup, dead-letter test) xong vì đụng nhiều file test đang xanh.

## Bài làm

Đã sửa theo đường A và tự kiểm chứng trên VM (harness chứa bản sao nguyên văn của `data/golden/cases.json` và cả 3 fixture chuẩn trên main): **5 passed**, chạy lẻ qua `__main__` cũng PASS. Negative control: đổi tên metric `post_video_views` → `total_video_views` trong script → 3 test guard đỏ ngay (builder mirror, cases, regeneration).

Nội dung fix trong `scripts/collect_sample_data.py` (chỉ đụng đúng phần đã chốt, `benchmark`/`main`/khung còn lại giữ nguyên):
- `facebook_raw()` sinh đúng shape connector sở hữu: `VIDEO_FIELDS` trên `data[0]` (id `987`, `length`, `views`, `likes/comments.summary`, `shares`) + `insights` **top-level** từ endpoint `video_insights` riêng với `post_video_views`/`post_video_view_time`/`post_impressions`.
- `tiktok_raw()` sinh đúng shape API thật: id `7512345678901234567`, counter lồng trong `stats` (`play_count`/`digg_count`/`comment_count`/`share_count`/`collect_count`), kèm `desc`, `cover_image_url`, `share_url`, `cursor: ""`, `has_more: false`.
- `normalize()` đọc đúng các tên raw đó (facebook: `post_video_view_time` là **tổng giây**, bỏ phép `//1000` trên metric trung bình cũ; tiktok: đọc từ `stats`), cho ra đúng metric chuẩn trong `data/golden/cases.json` — `build_golden_cases() == data/golden/cases.json` cho cả 3 platform, không còn views=0/PARTIAL.

File test mới khóa cả 3 tầng: builder == fixture chuẩn (parsed JSON, 3 platform), `build_golden_cases()` == `cases.json` trên đĩa (assert đã khôi phục), và chạy lại `main()` vào thư mục tạm thì cây golden sinh ra vẫn bằng cây chuẩn — máy tái tạo drift đã tắt. Assert arbiter chỉ-YouTube trong `test_fix_golden_unified_source.py` được thay thế bởi các assert 3-platform này và vẫn xanh nguyên trạng.

⚠ Hai việc bắt buộc đi cùng merge nhưng nằm NGOÀI phạm vi nộp của nhánh (chỉ được nộp script + file test riêng), xin CEO áp dụng trong cùng merge:
1. **`tests/test_backend_sec_m2.py`** (file của tôi ở vòng fix_missing_tests_critical_modules) còn assert số builder cũ qua `normalize()`/`build_golden_cases()`/`main()` — không dời là CI đỏ ngay khi script merge. Số cũ → số chuẩn: facebook `fb_video_001`/12000/800/120/45/watch 45 → **`987`/"T"/100/5/2/1/watch 1234**; tiktok `tt_video_001`/"Sample TikTok"/50000/4200/310/190/saves 260 → **`7512345678901234567`/"Meo tam su dem khuya"/1000/120/15/8/saves 20**; youtube giữ nguyên 98000/6100/540.
2. **`data/sample/*` trên main** vẫn là output cũ cho tới khi chạy lại `python scripts/collect_sample_data.py --out data/sample` và commit cây sinh ra (file dữ liệu nằm ngoài phạm vi nhánh). Guard test #5 đã chứng minh output sinh ra bằng cây chuẩn nên lần chạy này an toàn (chỉ `benchmark.csv` thay đổi số latency như thường lệ).

(đã nộp file `scripts/collect_sample_data.py`)

(đã nộp file `tests/test_fix_golden_path_a_script_alignment.py`)

## File đã nộp

- `scripts/collect_sample_data.py`
- `tests/test_fix_golden_path_a_script_alignment.py`
