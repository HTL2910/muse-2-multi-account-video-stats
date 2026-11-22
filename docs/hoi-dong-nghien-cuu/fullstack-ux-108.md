# Sửa lỗi `fix/fix_registry_dead_code`: Xóa src/registry.py rỗng bằng PR dọn dẹp riêng

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_registry_still_empty (muse) + fix_registry_dead_code (systems-arch) + fix_registry_empty_stub (backend-sec). File giống hệt ba vòng liên tiếp: APPROACH_REGISTRY = {} rỗng, hàm register_approach không có caller nào đăng ký approach_01..20, không module nào import — dead code tạo ấn tượng sai có cơ chế discovery trung tâm. Hội đồng đã chốt xóa từ vòng 1 (không nạp thật), nhưng ba lần giao xóa 'gộp vào fix wiring' đều bị bỏ sót bước cleanup cuối — lần này giao thành PR riêng để không bị chìm nữa. Fix: grep toàn repo xác nhận lần cuối không ai import src.registry, rồi git rm src/registry.py và mọi tham chiếu tới nó trong docs, trong PR dọn dẹp riêng không gộp fix logic.

## Bài làm

Việc của nhánh này là một **xóa file**, nên cách nộp khác thường lệ — xin CEO lưu ý cơ chế:

**Hành động yêu cầu: `git rm src/registry.py` — và tôi cố ý KHÔNG xuất khối `(đã nộp file `src/registry.py`)

## File đã nộp

- `src/registry.py`
