# Sửa lỗi `fix/fix_import_dual_contracts_residual`: PR cơ học toàn src/: xóa mọi fallback import contracts và mở rộng guard quét toàn cây

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp 4 báo cáo (muse fix_import_dual_contracts_still_partial, systems-arch fix_import_dual_contracts_residual_remai, backend-sec fix_import_dual_contracts_residual, fullstack-ux fix_import_dual_contracts_residual). Chỉ core và account_model đã sạch; fallback try/except ImportError vẫn còn ở ít nhất 8 module: src/ai/recommendation_engine.py, src/analytics/time_series.py, src/browser/profile_manager.py, src/observability/monitor.py, src/risk/account_health.py, src/connectors/facebook_connector.py, src/scheduler/task_scheduler.py, src/connectors/tiktok_connector.py (và grep lại src/api/main.py, src/api/alert_rule_engine.py, src/accounts/vault.py, src/connectors/youtube_connector.py). Nguy hiểm nhất là risk/account_health.py: fallback 'from account_model import AuditLog, transition' trỏ tới module top-level không tồn tại (đường đúng là src.accounts.account_model) nên nhánh except kích hoạt là ModuleNotFoundError ngay — bom hẹn giờ, không chỉ rủi ro identity Enum/dataclass khi cùng file nạp thành 2 module object. Fix: MỘT PR cơ học duy nhất cho toàn src/ — xóa mọi khối try/except ImportError fallback, chỉ giữ 'from src.contracts import ...' (risk dùng 'from src.accounts.account_model import ...'; facebook_connector dùng 'from src.core.models import ...'), chạy toàn bộ suite sau mỗi file, không sửa logic khác trong cùng PR. Mở rộng test guard: quét regex/AST trên mọi file src/**/*.py cấm 'from contracts import' và 'from ..contracts import' (guard hiện tại xanh dù fallback còn đầy vì nó chỉ kiểm tra 2 file đã sạch), kèm smoke test import mọi module từ repo root khẳng định không có module 'contracts' trần trong sys.modules. Sau khi dọn xong mới sửa docstring src/core/models.py bỏ lời hẹn 'PR tiếp theo'.

## Bài làm

**systems-arch — COUNCIL_FIX_WORK — `fix/fix_import_dual_contracts_residual` (file ai)**

Nộp đúng 2 file: `src/ai/recommendation_engine.py` + `tests/test_fix_import_dual_contracts_residual.py`. Không sửa logic nào khác.

**1. File code — chỉ sửa đúng khối import (mọi thứ còn lại giữ nguyên từng byte so với main):**
- Xóa khối `try: from src.contracts import (...) / except ImportError: from contracts import (...)`.
- Giữ duy nhất `from src.contracts import (Platform, Recommendation, UnifiedVideoMetric, Video, ViewForecast, compute_engagement_rate, utcnow)` — cùng danh sách tên như main.
- Docstring thêm ghi chú fix (không chứa chuỗi bị cấm ở dạng code — guard dùng AST nên không bắt nhầm).

**2. File test — guard quét toàn cây (AST, không phải grep):**
- `EXPECTED_CLEAN` (assert cứng, 8 file): `src/ai/recommendation_engine.py` (nhánh này), `src/core/models.py`, `src/accounts/account_model.py`, `src/ingestion/pipeline.py`, `src/worker/__main__.py`, `src/proxy/proxy_pool_manager.py`, `src/security/rbac_audit.py`, `src/scheduler/__main__.py` — tất cả đã xác minh bằng đọc trực tiếp trên main.
- `KNOWN_REMAINING` (11 file, `xfail(strict=False)`): `src/analytics/time_series.py`, `src/browser/profile_manager.py`, `src/observability/monitor.py`, `src/risk/account_health.py` (thêm fallback `from account_model import` trỏ tới module top-level không tồn tại), `src/connectors/facebook_connector.py`, `src/scheduler/task_scheduler.py`, `src/connectors/tiktok_connector.py`, `src/api/main.py` (fallback cả ở top-level lẫn lồng trong `_enqueue`), `src/api/alert_rule_engine.py`, `src/accounts/vault.py`, `src/connectors/youtube_connector.py` — tất cả đã xác minh bằng đọc trực tiếp trên main. XPASS khi một nhánh sau sửa xong là bình thường; nhánh đó chuyển file sang `EXPECTED_CLEAN`.
- `test_no_new_fallback_files`: quét mọi `src/**/*.py`, tập file có fallback phải là tập con của `KNOWN_REMAINING` — file mới có fallback (hoặc inventory sót) sẽ fail và nêu tên.
- Smoke: import từ repo root, `"contracts" not in sys.modules`, identity `is` giữa `engine.*` và `contracts.*`, và smoke chức năng của engine (`tokenize`, `tfidf_vectors`, `kmeans`, `cluster_videos`, `forecast_views`, `best_posting_hours`, `recommend`).
- Regression cho bẫy đã gặp ở vòng core: guard dùng AST nên docstring/comment mô tả fallback không bị bắt nhầm — test tự kiểm chứng trên chính file test này.

**3. Kiểm chứng trên VM (chưa chạy trên main — kết quả thật để CI):** dựng replica `~/workspace/fix-import3/` mô phỏng đúng cây `src/` trên main (stub contracts local-only, không nộp; các file inventory là placeholder mang đúng khối import thật của main). Kết quả: **12 passed, 11 xfailed**. Kiểm tra âm tính: (a) hoàn nguyên engine về bản fallback → đúng 2 test fail (`test_expected_clean_files[recommendation_engine]`, `test_no_new_fallback_files`); (b) thêm một file mới có fallback ngoài inventory → `test_no_new_fallback_files` fail và nêu tên cả hai file. Khôi phục lại → 12 passed, 11 xfailed. Test này chỉ đọc `src/` từ đĩa và tự bootstrap `sys.path` về repo root nên chạy được trong sandbox CI (nơi `tests/` chỉ có một mình file này).

**4. Ngoài phạm vi nhánh này (ghi chú để hội đồng giao nhánh tiếp theo, không nộp ở đây):** dọn 11 file còn lại trong `KNOWN_REMAINING`, mỗi file một nhánh; chỉ sau khi toàn cây sạch mới sửa docstring `src/core/models.py` bỏ lời hẹn "PR tiếp theo". Ghi chú thêm: `src/ingestion/pipeline.py` và `src/connectors/facebook_connector.py` còn một fallback tương đối `..core.models` (không phải contracts) — cùng họ rủi ro identity nhưng ngoài scope guard đã thống nhất, để làm follow-up riêng. File test này **thay thế** bản core-only cùng tên đã nộp ở vòng trước (core/models.py nay nằm trong `EXPECTED_CLEAN`).

(đã nộp file `src/ai/recommendation_engine.py`)

(đã nộp file `tests/test_fix_import_dual_contracts_residual.py`)

## File đã nộp

- `src/ai/recommendation_engine.py`
- `tests/test_fix_import_dual_contracts_residual.py`
