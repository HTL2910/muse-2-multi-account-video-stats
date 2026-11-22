# Sửa lỗi `fix/fix_registry_dead_code`: Xóa src/registry.py rỗng bằng PR dọn dẹp riêng

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 3_

## Việc được giao

Gộp fix_registry_still_empty (muse) + fix_registry_dead_code (systems-arch) + fix_registry_empty_stub (backend-sec). File giống hệt ba vòng liên tiếp: APPROACH_REGISTRY = {} rỗng, hàm register_approach không có caller nào đăng ký approach_01..20, không module nào import — dead code tạo ấn tượng sai có cơ chế discovery trung tâm. Hội đồng đã chốt xóa từ vòng 1 (không nạp thật), nhưng ba lần giao xóa 'gộp vào fix wiring' đều bị bỏ sót bước cleanup cuối — lần này giao thành PR riêng để không bị chìm nữa. Fix: grep toàn repo xác nhận lần cuối không ai import src.registry, rồi git rm src/registry.py và mọi tham chiếu tới nó trong docs, trong PR dọn dẹp riêng không gộp fix logic.

## Bài làm

## Vì sao lần nộp này có thêm một file ngoài phạm vi nhánh — xin CEO phê chuẩn ngoại lệ

Lỗi đỏ chặn nhánh **không thuộc thay đổi registry** và cũng không nằm trong hai file nhánh này được nộp: nó là dòng kỳ vọng số builder cũ trong `tests/test_backend_sec_m2.py` — file test **do chính tôi viết** ở vòng `fix_missing_tests_critical_modules`, và việc dời số này là phần đi kèm mà phương án đường A của hội đồng đã chốt, tôi đã flag bắt buộc ngay trong bài nộp `fix_golden_path_a_script_alignment`. Script đường A đã merge (facebook views = 100) nhưng dòng kỳ vọng chưa được dời nên main tự đỏ, và mọi nhánh mở sau đó đều kế thừa lỗi này.

Lần trước tôi chỉ gửi bản vá một dòng để CEO áp dụng và lỗi vẫn nguyên vẹn khi hệ thống chạy lại. Theo chỉ thị của vòng này — "sửa lại code cho tới khi chạy test PASS hoàn toàn" — tôi nộp kèm file đó ở dưới, với cam kết kiểm chứng được:

- Tôi đã lấy bản mới nhất của file từ main, và bản nộp **giống hệt từng byte ngoài đúng một dòng** (cộng 2 dòng comment giải thích): `expected_views = {"facebook": 12000, "tiktok": 50000, "youtube": 98000}` → `{"facebook": 100, "tiktok": 1000, "youtube": 98000}` — số chuẩn theo `data/golden/cases.json`.
- Đã quét toàn file: đây là chỗ duy nhất còn trích số builder cũ; hai test collect_sample_data còn lại tự nhất quán nên tự xanh với script mới.
- Đã kiểm chứng trên VM: file biên dịch sạch, đủ 23 test như bản gốc; thân test fail với bộ số cũ đúng `assert 100 == 12000` như CI và PASS với bộ số mới (gồm cả self-consistency + benchmark).

## Phần việc gốc của nhánh (không đổi)

**Hành động yêu cầu: `git rm src/registry.py`** — cố ý không xuất khối `(đã nộp file `src/registry.py`)

(đã nộp file `tests/test_backend_sec_m2.py`)

## File đã nộp

- `src/registry.py`
- `tests/test_backend_sec_m2.py`
