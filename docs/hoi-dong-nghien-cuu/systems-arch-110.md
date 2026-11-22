# Sửa lỗi `fix/fix_core_store_return_type`: Merge nhánh core store trả VideoStatsSnapshot và sửa docstring pipeline sau cùng

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_core_store_return_type_missing (muse) + fix_core_store_return_type_unmerged (systems-arch). Docstring src/core/models.py tự thừa nhận InMemoryMetricsStore.save_snapshot() vẫn trả metric trần thay vì VideoStatsSnapshot — lệch protocol MetricsStore, mọi caller trực tiếp (không qua lớp bọc defensive của pipeline) nhận sai kiểu. Tệ hơn, docstring src/ingestion/pipeline.py khẳng định store 'delegates to src.core.models.InMemoryMetricsStore (the canonical implementation after fix_core_store_return_type)' — tài liệu nói sai trạng thái merge, đúng cách bệnh mất-merge tự che giấu. Fix: merge nhánh fix_core_store_return_type đã nộp (save_snapshot trả và lưu VideoStatsSnapshot, idempotent theo idempotency_key giờ); cập nhật mọi caller trong core/pipeline/time_series/API theo kiểu mới; sửa docstring pipeline bỏ cụm 'after fix_core_store_return_type' SAU CÙNG, chỉ khi code thật đã lên main (theo thứ tự backend-sec: merge code + caller trước, docstring sau). Kiểm chứng manifest: tests/test_fix_core_store_return_type.py phải có mặt trên main. Test: save_snapshot trả về isinstance VideoStatsSnapshot và mang đúng snapshot_key theo giờ.

## Bài làm

**systems-arch — COUNCIL_FIX_WORK — `fix/fix_core_store_return_type`**

Nộp đúng 2 file: `src/core/models.py` + `tests/test_fix_core_store_return_type.py`.

**1. File code — rebase bản fix đã nộp lên đúng main hiện tại (đã gồm fix import-cleanup):**
- Import: giữ nguyên khối `from src.contracts import (...)` duy nhất của main, chỉ thêm `VideoStatsSnapshot` vào danh sách (không khôi phục fallback hai kiểu import của bản fix cũ).
- `InMemoryMetricsStore.save_snapshot()` giờ trả **và lưu** `VideoStatsSnapshot(id=new_id(), metric=metric, raw_payload_ref="")` đúng protocol `MetricsStore` — không còn trả metric trần. Idempotent theo `metric.idempotency_key` (snapshot_key theo giờ, contracts 1.1.0): cùng key thì trả đúng snapshot cũ, không ghi đè, không tạo trùng. Vẫn `raise ValueError` khi `validate_metric` báo lỗi. `get_history()` / `get_latest()` / `daily_rollup()` vẫn trả `UnifiedVideoMetric` như trước; `get_latest` trả phần tử append cuối cùng.
- Docstring: ghi chú cũ "save_snapshot() vẫn trả metric trần... chưa merge" được thay bằng ghi chú fix; giữ nguyên các ghi chú của fix import-cleanup, kể cả lời hẹn "PR tiếp theo" (thuộc về PR import, không sửa ở nhánh này). Mọi phần còn lại của file giữ nguyên từng byte so với main — diff với bản fix trước chỉ khác phần header, phần thân giống hệt.

**2. File test (6 test, giữ nguyên bản đã sửa sau CI fail lần trước):**
- `save_snapshot` trả `isinstance VideoStatsSnapshot`, `result.metric is metric`, `id` mới không rỗng, `raw_payload_ref == ""`.
- Idempotent: lưu 2 lần cùng key trả đúng cùng object, history chỉ 1 điểm.
- History/latest/rollup vẫn trả metric: snapshot thứ 2 ở giờ khác (2026-11-23 10:00) được giữ, `get_latest` là 300, rollup ngày 2026-11-22 là 100.
- Hồi quy idempotency theo giờ: cùng hour_bucket thì dedup ngay cả khi `date_bucket` khác (chính là lỗi fixture đã làm fail CI lần trước — test sai, không phải code sai).
- Metric không hợp lệ (`views=-1`) vẫn bị reject trước khi snapshot; truy cập kiểu protocol `result.metric.<field>` không lỗi (`engagement_rate == 0.08`, `idempotency_key` có giá trị).

**Kiểm chứng trên VM (chưa chạy trên main — kết quả thật để CI):** replica `~/workspace/fix-core2/` với stub contracts local-only (không nộp): **6 passed**. Kiểm tra âm tính: hoàn nguyên `save_snapshot` về bản trả metric trần của main → **3 failed, 3 passed** (đúng 3 test bắt kiểu trả về đỏ).

**Ngoài phạm vi nhánh này (bước merge, theo đúng thứ tự hội đồng đã chốt):** cập nhật mọi caller của `save_snapshot` trong time_series/API sang dùng `.metric` (lớp bọc defensive của pipeline đã chịu được cả hai kiểu nên không cần sửa gấp); và **chỉ sau khi code thật đã lên main** mới sửa docstring `src/ingestion/pipeline.py` bỏ cụm "after fix_core_store_return_type" — không nộp pipeline.py ở nhánh này.

(đã nộp file `src/core/models.py`)

(đã nộp file `tests/test_fix_core_store_return_type.py`)

## File đã nộp

- `src/core/models.py`
- `tests/test_fix_core_store_return_type.py`
