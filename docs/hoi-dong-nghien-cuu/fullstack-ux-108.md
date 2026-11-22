# Sửa lỗi `fix/fix_registry_dead_code`: Xóa src/registry.py rỗng bằng PR dọn dẹp riêng

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 2_

## Việc được giao

Gộp fix_registry_still_empty (muse) + fix_registry_dead_code (systems-arch) + fix_registry_empty_stub (backend-sec). File giống hệt ba vòng liên tiếp: APPROACH_REGISTRY = {} rỗng, hàm register_approach không có caller nào đăng ký approach_01..20, không module nào import — dead code tạo ấn tượng sai có cơ chế discovery trung tâm. Hội đồng đã chốt xóa từ vòng 1 (không nạp thật), nhưng ba lần giao xóa 'gộp vào fix wiring' đều bị bỏ sót bước cleanup cuối — lần này giao thành PR riêng để không bị chìm nữa. Fix: grep toàn repo xác nhận lần cuối không ai import src.registry, rồi git rm src/registry.py và mọi tham chiếu tới nó trong docs, trong PR dọn dẹp riêng không gộp fix logic.

## Bài làm

## Chẩn đoán lỗi đỏ: không nằm ở thay đổi registry

Tôi đã đọc toàn bộ `tests/test_backend_sec_m2.py` trên main và tái hiện lỗi trên VM. Kết luận chắc chắn:

- Test đỏ `test_collect_sample_data_golden_cases_cover_all_platforms` fail vì **đúng một dòng kỳ vọng cũ** — đây là việc đi kèm của `fix_golden_path_a_script_alignment` mà tôi đã flag bắt buộc trong bài nộp đường A ("không dời là CI đỏ ngay khi script merge"). Script đường A đã merge vào main (facebook views giờ là 100) nhưng dòng kỳ vọng trong file test này chưa được dời, nên **main hiện tại tự nó đã đỏ ở test này**, không phụ thuộc nhánh registry — test của nhánh này chỉ là nơi nó lộ ra.
- Quét toàn file: đây là chỗ DUY NHẤT còn trích số builder cũ. Hai test collect_sample_data còn lại đều tự nhất quán (`case == normalize(builder)`, `raw == builder()`, `cases == build_golden_cases()`) nên tự xanh với script mới; assert `"example.invalid"` vẫn thỏa nhờ thumbnail YouTube.

**Bản vá chính xác một dòng** trong `tests/test_backend_sec_m2.py` (hàm `test_collect_sample_data_golden_cases_cover_all_platforms`) — file này nằm ngoài phạm vi nộp của nhánh `fix/fix_registry_dead_code` (chỉ `src/registry.py` + test riêng) nên tôi không nộp kèm được, xin CEO áp dụng trực tiếp (đây là file tôi viết ở vòng fix_missing_tests_critical_modules, việc dời số này thuộc merge đường A):

```python
# cũ:
expected_views = {"facebook": 12000, "tiktok": 50000, "youtube": 98000}
# mới (số chuẩn theo data/golden/cases.json):
expected_views = {"facebook": 100, "tiktok": 1000, "youtube": 98000}
```

Đã kiểm chứng trên VM với script mới trên main: bộ kỳ vọng cũ fail đúng `assert 100 == 12000` như CI; bộ kỳ vọng mới PASS toàn bộ thân test (gồm cả self-consistency và benchmark).

## Phần việc của nhánh này (giữ nguyên, đã PASS ở lần chạy trước)

**Hành động yêu cầu: `git rm src/registry.py`** — cố ý không xuất khối `(đã nộp file `src/registry.py`)

Sau khi CEO áp dụng dòng vá `expected_views` ở trên, toàn bộ suite sẽ xanh: phần registry của nhánh này vốn đã pass độc lập, và test đỏ duy nhất còn lại chính là dòng kỳ vọng đó.

## File đã nộp

- `src/registry.py`
