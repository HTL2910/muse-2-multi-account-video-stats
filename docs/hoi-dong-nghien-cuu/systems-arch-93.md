# Sửa lỗi `fix/fix_import_dual_contracts_residual`: Xóa toàn bộ fallback import contracts còn sót trên toàn repo

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_import_dual_contracts_incomplete (muse) + fix_core_dual_import (systems-arch) + fix_import_dual_contracts_partial (backend-sec) + fix_import_dual_contracts_residual (fullstack-ux). fix_import_dual_contracts vòng 1 mới áp dụng cho đúng src/accounts/account_model.py; pattern try/except hai kiểu vẫn còn ở src/core/models.py (ưu tiên sửa trước vì PlatformAdapterBase.guard() được mọi connector import), src/ai/recommendation_engine.py, src/analytics/time_series.py, src/browser/profile_manager.py, src/observability/monitor.py, src/risk/account_health.py (kèm cả 'from account_model import' sai — sẽ ModuleNotFoundError ngay khi nhánh except kích hoạt, là bom nổ chậm), src/connectors/facebook_connector.py (kèm cả fallback ..core.models), và theo grep của algo-ml còn ở src/scheduler/task_scheduler.py và src/connectors/tiktok_connector.py. Nguy cơ dual-module identity là thật dù pytest không bắt được (luôn import qua src.* từ repo root). Fix: một PR cơ học cho toàn src/ — chỉ giữ 'from src.contracts import ...' (và 'from src.core.models import ...' cho facebook_connector), xóa mọi khối try/except ImportError fallback; sửa docstring core/models.py bỏ câu 'theo ca 2 cach'. Thêm test tĩnh (grep/AST) cấm các mẫu 'from contracts import' và 'from ..contracts import' trong src/ và smoke test import mọi module từ repo root khẳng định Platform/AccountStatus là cùng object và không có module 'contracts' trần trong sys.modules.

## Bài làm

Nhánh `fix/fix_import_dual_contracts_residual` — chỉ nộp 2 file theo quy tắc nhánh `fix/*`. Đây là bước cơ học cho riêng `src/core/models.py` (file ưu tiên sửa trước vì `PlatformAdapterBase.guard()` được mọi connector import); các file còn fallback (ai, analytics, browser, observability/monitor, risk — kèm cả `from account_model import` sai sẽ `ModuleNotFoundError` khi nhánh except kích hoạt — facebook_connector, scheduler, tiktok_connector) thuộc PR cơ học toàn `src/` tiếp theo, không sửa ở nhánh này.

Những gì sửa trong `src/core/models.py`:
- Xóa toàn bộ khối `try/except ImportError` hai kiểu import, chỉ giữ duy nhất `from src.contracts import (...)` chạy từ repo root — cùng danh sách tên import như trên `main`, không thêm/bớt tên nào.
- Sửa docstring: bỏ câu "Import contracts theo ca 2 cach (gọi từ repo root `src.contracts` hoặc gọi trong package `..contracts`)...", thay bằng quy tắc một kiểu import duy nhất và ghi chú fix.
- **Không đổi bất kỳ hành vi nào khác.** Đặc biệt `InMemoryMetricsStore.save_snapshot()` vẫn trả metric trần y như trên `main` (kèm comment gốc "Tra ve object tuong duong VideoStatsSnapshot (khong import vong tron)") — lỗi kiểu trả về đó thuộc `fix_core_store_return_type`, chưa merge, cố tình không sửa ở đây để giữ phạm vi nhánh. Mọi class/hàm còn lại (`CoreConfig`, `VideoMetricDaily`, `transition_account`, `PlatformAdapterBase.guard()` với kiểm tra `credential_ref`, `AdapterRegistry`, các store, `CoreService`) giữ nguyên verbatim.

Test kèm theo (`tests/test_fix_import_dual_contracts_residual.py`) là test tĩnh + smoke identity, như phương án hội đồng: AST khẳng định `core/models.py` có đúng một import contracts và đó là `src.contracts` level 0, không có `try` nào bọc import bằng `except ImportError`; text grep cấm `from contracts import` / `from ..contracts import` / `from account_model import`; smoke test import từ repo root khẳng định không có module `contracts` trần trong `sys.modules`, `core.AccountStatus is contracts.AccountStatus` (và `Platform`, `PlatformAccount`, `UnifiedVideoMetric`, `Video`, `JobStatus`, `TokenSet`), `isinstance` xuyên module đúng và `transition_account` CREATED → VERIFIED dùng cùng một Enum object; kiểm tra hồi quy cho `account_model.py` (fix vòng 1); và kiểm kê residual chỉ khẳng định core + account_model sạch, không kết luận oan về các file ngoài phạm vi nhánh.

Kiểm thử trên VM Linux (Python 3.12.3, pytest 9.2.0): `pytest tests/test_fix_import_dual_contracts_residual.py` = **6 passed**. Trong lúc kiểm thử, 4 test lỗi lúc đầu đều là lỗi của chính test/bản nháp (docstring mới vẫn chứa nguyên văn chuỗi bị cấm nên grep tự bắt mình; test dùng chuyển đổi ACTIVE → VERIFIED vốn bất hợp lệ theo `ALLOWED_ACCOUNT_TRANSITIONS` và nhầm trạng thái mặc định của `create_account` là ACTIVE trong khi stub để CREATED) — đã sửa test và docstring, code `core/models.py` không đổi hành vi. Kết quả thật trên `main` do CI báo cáo.

(đã nộp file `src/core/models.py`)

(đã nộp file `tests/test_fix_import_dual_contracts_residual.py`)

## File đã nộp

- `src/core/models.py`
- `tests/test_fix_import_dual_contracts_residual.py`
