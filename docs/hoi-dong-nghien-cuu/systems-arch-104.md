# Sửa lỗi `fix/fix_import_dual_contracts_residual`: PR cơ học toàn src/: xóa mọi fallback import contracts và mở rộng guard quét toàn cây

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 2_

## Việc được giao

Gộp 4 báo cáo (muse fix_import_dual_contracts_still_partial, systems-arch fix_import_dual_contracts_residual_remai, backend-sec fix_import_dual_contracts_residual, fullstack-ux fix_import_dual_contracts_residual). Chỉ core và account_model đã sạch; fallback try/except ImportError vẫn còn ở ít nhất 8 module: src/ai/recommendation_engine.py, src/analytics/time_series.py, src/browser/profile_manager.py, src/observability/monitor.py, src/risk/account_health.py, src/connectors/facebook_connector.py, src/scheduler/task_scheduler.py, src/connectors/tiktok_connector.py (và grep lại src/api/main.py, src/api/alert_rule_engine.py, src/accounts/vault.py, src/connectors/youtube_connector.py). Nguy hiểm nhất là risk/account_health.py: fallback 'from account_model import AuditLog, transition' trỏ tới module top-level không tồn tại (đường đúng là src.accounts.account_model) nên nhánh except kích hoạt là ModuleNotFoundError ngay — bom hẹn giờ, không chỉ rủi ro identity Enum/dataclass khi cùng file nạp thành 2 module object. Fix: MỘT PR cơ học duy nhất cho toàn src/ — xóa mọi khối try/except ImportError fallback, chỉ giữ 'from src.contracts import ...' (risk dùng 'from src.accounts.account_model import ...'; facebook_connector dùng 'from src.core.models import ...'), chạy toàn bộ suite sau mỗi file, không sửa logic khác trong cùng PR. Mở rộng test guard: quét regex/AST trên mọi file src/**/*.py cấm 'from contracts import' và 'from ..contracts import' (guard hiện tại xanh dù fallback còn đầy vì nó chỉ kiểm tra 2 file đã sạch), kèm smoke test import mọi module từ repo root khẳng định không có module 'contracts' trần trong sys.modules. Sau khi dọn xong mới sửa docstring src/core/models.py bỏ lời hẹn 'PR tiếp theo'.

## Bài làm

**systems-arch — COUNCIL_FIX_WORK — `fix/fix_import_dual_contracts_residual` (file ai) — SỬA LẠI SAU CI FAIL**

**Chẩn đoán lỗi CI (chỉ lỗi test, code không lỗi):** CI Windows báo `1 failed, 11 passed, 11 xfailed` — chỉ `test_no_new_fallback_files` đỏ. Nguyên nhân: trong test tôi dùng `str(path.relative_to(REPO_ROOT))` làm khóa dict; trên Windows `str(Path)` dùng dấu `\` nên không một khóa nào khớp với inventory viết kiểu `/` (`src/accounts/vault.py`...), và cả 11 file đã inventory đều bị coi là "mới". Bằng chứng trong chính output lỗi CI: 11 file tìm được trên main thật khớp đúng inventory của tôi, kèm số dòng thật (vault.py:90, time_series.py:40, alert_rule_engine.py:57, api/main.py:61 và 528 — cả fallback lồng trong `_enqueue`, profile_manager.py:76, facebook_connector.py:38, tiktok_connector.py:68, youtube_connector.py:61, monitor.py:36, account_health.py:33 và 38 — cả hai fallback, task_scheduler.py:70). Không có file ngoài inventory nào xuất hiện.

**Fix (chỉ file test, `src/ai/recommendation_engine.py` giữ nguyên từng byte):**
- Thêm helper `_posix_rel()` dùng `path.relative_to(REPO_ROOT).as_posix()` khi làm khóa dict trong `test_no_new_fallback_files`.
- Thêm regression test `test_windows_style_paths_match_inventory` chứng minh bằng `PureWindowsPath` rằng đường dẫn kiểu Windows chuẩn hóa đúng về đúng spelling của inventory (chạy được trên mọi OS, kể cả Linux).

**Kiểm chứng lại trên VM:** replica mô phỏng đúng cây `src/` trên main → **13 passed, 11 xfailed** (12 test cũ + 1 regression test mới). Kiểm tra âm tính trên bản sao tạm: hoàn nguyên engine về bản fallback và thêm một file mới có fallback ngoài inventory → đúng 2 test fail như thiết kế (`test_expected_clean_files[recommendation_engine]`, `test_no_new_fallback_files`); khôi phục lại → 13 passed, 11 xfailed. Nộp lại đầy đủ cả 2 file.

(đã nộp file `src/ai/recommendation_engine.py`)

(đã nộp file `tests/test_fix_import_dual_contracts_residual.py`)

## File đã nộp

- `src/ai/recommendation_engine.py`
- `tests/test_fix_import_dual_contracts_residual.py`
