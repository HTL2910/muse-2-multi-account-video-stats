# Sửa lỗi `fix/fix_pipeline_fail_closed_wiring`: Khôi phục pipeline wiring: gate fail-closed, một store chung, xóa registry

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_pipeline_no_fail_closed_gate_duplica (systems-arch) + fix_registry_still_empty/fix_registry_empty_stub (muse + backend-sec) + fix_ingestion_daily_key_reference (fullstack-ux, đã hạ mức). Bản main vẫn chưa có gate: run() gọi thẳng adapter.list_videos() mà không kiểm tra FAIL_CLOSED_STATUSES hay proxy nên account CHECKPOINT/BANNED vẫn bị crawl; InMemoryMetricsStore của pipeline là bản copy thứ ba (sau core và time_series) không có get_shared_store() nên sync xong dashboard có thể vẫn rỗng; docstring vẫn ghi dedup theo ngày trong khi contracts 1.1.0 đã đổi idempotency_key sang theo giờ; src/registry.py vẫn là APPROACH_REGISTRY={} trống. Fix: merge lại nhánh fix_unified_store_pipeline_wiring đã nộp — gate ngay đầu run() (account fail-closed hoặc proxy không ACTIVE -> PipelineReport REJECT + JobResult REJECTED, không gọi adapter); InMemoryMetricsStore của pipeline subclass core store + get_shared_store() làm điểm nối DI duy nhất cho scheduler/API; dedup trong run theo metric.idempotency_key (theo giờ); về ingestion daily-key: theo kiểm chứng trên VM của algo-ml, build_idempotency_key(..., date_bucket) hiện chỉ dùng cho seen_keys trong một lần run (set cục bộ mỗi run() tạo mới), store dedup bằng metric.idempotency_key theo giờ nên KHÔNG mất snapshot theo giờ — vẫn đổi seen_keys sang metric.idempotency_key/build_snapshot_idempotency_key cho nhất quán và sửa docstring bỏ '(account, platform_video, day)'; thêm test chéo quyết định: hai snapshot cùng video cách nhau 2 giờ trong cùng ngày phải cùng được lưu (2 điểm), hai snapshot cùng giờ mới dedup thành 1 — test này hoặc chứng minh lỗi hoặc đóng vụ daily-key vĩnh viễn. Xóa src/registry.py sau khi grep xác nhận không ai import (đây là cách xử lý registry, không lập nhánh riêng). Test tích hợp: enqueue sync -> pipeline chạy với fake adapter -> GET /videos/{id}/metrics thấy snapshot.

## Bài làm

Nhánh `fix/fix_pipeline_fail_closed_wiring` — chỉ nộp 2 file theo quy tắc nhánh `fix/*`. Đây là bản merge lại nhánh `fix_unified_store_pipeline_wiring` đã nộp trước, rebase lên bản `main` hiện tại (đã có `fix_ingestion_normalize_removed`: `_to_int`/`_to_float`, unparseable → PARTIAL, watch-time giây > ms > phút, `_known` nạp từ store — tất cả giữ nguyên verbatim).

Những gì sửa trong `src/ingestion/pipeline.py`:
- **Gate fail-closed ngay đầu `run()`**: account ở `FAIL_CLOSED_STATUSES` hoặc proxy bound không `ACTIVE` (qua tham số `proxy=` hoặc `proxy_resolver` ở `__init__`, resolver trả `None` nghĩa là không có proxy bound) thì trả `PipelineReport(decision=REJECT)` và **không gọi adapter**. `to_job_result()` map `REJECT` → `JobStatus.REJECTED` (trước đây rơi vào `FAILED`).
- **Một store duy nhất**: `InMemoryMetricsStore` của pipeline giờ subclass `src.core.models.InMemoryMetricsStore` (bản canonical), chỉ giữ thêm `__len__`, `get_latest()` theo `collected_at` lớn nhất và `known_platform_video_ids()` (nguồn sự thật cho phát hiện video bị xóa — đã kiểm tra vẫn hoạt động qua store chung); `save_snapshot()` vẫn bọc về `VideoStatsSnapshot` nếu core cũ còn trả metric trần. Thêm `get_shared_store()` — một instance chung cho scheduler worker, pipeline và API qua DI, để sync không còn ghi vào store khác với store dashboard đọc.
- **Dedup theo giờ**: `seen_keys` trong một lần run dùng `metric.idempotency_key` (bucket giờ, contracts 1.1.0), không dùng `build_idempotency_key` theo ngày nữa; docstring sửa bỏ `(account, platform_video, day)`. Test chéo quyết định đã chứng minh: snapshot 10:00 và 12:00 cùng ngày đều được giữ (2 điểm), chỉ 12:30 cùng giờ mới dedup thành 1 — đúng như kiểm chứng của algo-ml rằng store vốn đã dedup theo giờ nên không mất snapshot nào, thay đổi này chỉ làm run-level nhất quán với store.
- **Máy trạng thái**: pipeline không tự chuyển trạng thái account; bản canonical là `src.accounts.account_model.transition`. **Xóa `src/registry.py` không nộp được trong nhánh này**: test kèm theo grep chứng minh không file `src/` nào import nó, việc xóa là `git rm src/registry.py` ở bước merge.

Những gì **không** nộp ở nhánh này (thuộc file khác, cần nhánh/merge riêng): `create_app` trong `api/main.py` phải dùng `get_shared_store()` thay vì store riêng, scheduler phải truyền `proxy_resolver`/`proxy` vào pipeline, và `core.transition_account` delegate về `account_model.transition`.

Kiểm thử trên VM Linux (Python 3.12.3, pytest 9.2.0) với fake adapter: `pytest tests/test_fix_pipeline_fail_closed_wiring.py` = **14 passed**, gồm gate cho 5 trạng thái fail-closed, proxy QUARANTINED/không bound, test chéo 2 giờ/2 điểm + cùng giờ dedup 1, phát hiện video bị xóa qua store chung, normalize `views='abc'` vẫn PARTIAL sau rebase, và grep registry. Trong lúc kiểm thử, stub contracts tại chỗ thiếu field `ctr` mà contracts 1.1.0 thật có nên đã bổ sung cho stub khớp (stub chỉ để kiểm thử, không nộp). Kết quả thật trên `main` do CI báo cáo.

(đã nộp file `src/ingestion/pipeline.py`)

(đã nộp file `tests/test_fix_pipeline_fail_closed_wiring.py`)

## File đã nộp

- `src/ingestion/pipeline.py`
- `tests/test_fix_pipeline_fail_closed_wiring.py`
