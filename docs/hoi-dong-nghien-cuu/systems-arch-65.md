# Sửa lỗi `fix/fix_unified_store_pipeline_wiring`: Thống nhất một MetricsStore và một máy trạng thái, nối pipeline-API, xóa registry chết

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_duplicate_state_machine_and_stores_n (systems-arch) + fix_pipeline_no_fail_closed_and_unwired_ (fullstack-ux). Có hai máy trạng thái song song (core.transition_account vs accounts.transition, kiểu lỗi và cách ghi audit khác nhau) và ít nhất ba InMemoryMetricsStore + InMemoryStore riêng của API; pipeline.run() gọi thẳng adapter mà không kiểm tra FAIL_CLOSED_STATUSES/proxy nên account CHECKPOINT/BANNED vẫn bị crawl; không có code nào bơm kết quả pipeline/scheduler vào store của API nên sync xong dashboard vẫn rỗng; src/registry.py chỉ có APPROACH_REGISTRY={} trống. Fix: chọn một bản canonical cho transition (account_model.transition) và một MetricsStore duy nhất sau protocol contracts.MetricsStore, các bản khác delegate về nó; gate ngay đầu pipeline.run: account ở FAIL_CLOSED_STATUSES hoặc proxy không ACTIVE thì trả PipelineReport(decision=REJECT) + JobResult REJECTED, không gọi adapter; nối scheduler worker -> pipeline -> store chung -> API đọc cùng store qua DI trong create_app; thêm entry point compose (app factory) dùng chung store/scheduler/adapter; xóa src/registry.py sau khi grep xác nhận không nơi nào import (đây là cách xử lý fix_unplanned_registry, không lập nhánh riêng). Test tích hợp: enqueue sync -> pipeline chạy với fake adapter -> GET /videos/{id}/metrics thấy snapshot.

## Bài làm

Nhánh `fix/fix_unified_store_pipeline_wiring` — chỉ nộp 2 file theo quy tắc nhánh `fix/*`.

Những gì làm được trong phạm vi 1 file `pipeline.py`:
- **Gate fail-closed ngay đầu `run()`**: account ở `FAIL_CLOSED_STATUSES` hoặc proxy bound không `ACTIVE` (qua tham số `proxy=` hoặc `proxy_resolver` ở `__init__`, resolver trả `None` nghĩa là không có proxy bound) thì trả `PipelineReport(decision=REJECT)` và **không gọi adapter**. `to_job_result()` map `REJECT` → `JobStatus.REJECTED` (trước đây rơi vào `FAILED`).
- **Một store duy nhất**: `InMemoryMetricsStore` của pipeline giờ subclass `src.core.models.InMemoryMetricsStore` (bản canonical sau `fix_core_store_return_type`), chỉ giữ thêm `__len__` và `get_latest()` theo `collected_at` lớn nhất; `save_snapshot()` vẫn bọc về `VideoStatsSnapshot` nếu core cũ còn trả metric trần. Thêm `get_shared_store()` — một instance chung cho scheduler worker, pipeline và API qua DI, để sync không còn ghi vào store khác với store dashboard đọc.
- **Dedup trong run theo `metric.idempotency_key` (theo giờ)**, không theo ngày như cũ — nếu không snapshot giờ thứ hai trong ngày bị loại oan.
- **Máy trạng thái**: pipeline không tự chuyển trạng thái account; ghi rõ bản canonical là `src.accounts.account_model.transition` (core delegate theo nó là việc của file core, không sửa ở đây). **Xóa `src/registry.py` không nộp được trong nhánh này** (chỉ được nộp pipeline + test): test kèm theo grep chứng minh không file `src/` nào import nó, việc xóa là `git rm src/registry.py` ở bước merge.

Những gì **không** nộp ở nhánh này (thuộc file khác, cần nhánh/merge riêng): `create_app` trong `api/main.py` phải dùng `get_shared_store()` thay vì store riêng, scheduler phải truyền `proxy_resolver`/`proxy` vào pipeline, và `core.transition_account` delegate về `account_model.transition`.

Kiểm thử trên VM Linux (Python 3.12.3, pytest 9.2.0) với fake adapter: `pytest tests/test_fix_unified_store_pipeline_wiring.py` = **11 passed**. Trong lúc kiểm thử, stub contracts tại chỗ đã bắt được 2 giá trị thật của contract mà stub cũ thiếu (`VideoStatus.DELETED`, `DataQuality.NEEDS_REVIEW` — pipeline gốc dùng cả hai) và đã sửa stub cho khớp; stub chỉ để kiểm thử, không nộp. Kết quả thật trên `main` do CI báo cáo.

(đã nộp file `src/ingestion/pipeline.py`)

(đã nộp file `tests/test_fix_unified_store_pipeline_wiring.py`)

## File đã nộp

- `src/ingestion/pipeline.py`
- `tests/test_fix_unified_store_pipeline_wiring.py`
