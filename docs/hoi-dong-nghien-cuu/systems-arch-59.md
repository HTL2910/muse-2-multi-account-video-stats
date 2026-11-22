# Sửa lỗi `fix/fix_core_store_return_type`: Core InMemoryMetricsStore.save_snapshot trả VideoStatsSnapshot theo protocol

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 2_

## Việc được giao

Gộp fix_core_metrics_store_return_type (systems-arch, đã kiểm chứng VM) + fix_metrics_store_return_type_mismatch (algo-ml). Contracts MetricsStore.save_snapshot() khai báo trả VideoStatsSnapshot, ingestion/analytics trả đúng kiểu, nhưng core trả thẳng UnifiedVideoMetric nên code gọi qua protocol và truy cập .metric sẽ AttributeError và các store không thể thay thế nhau. Fix: trả VideoStatsSnapshot(id=new_id(), metric=metric, raw_payload_ref='') và lưu snapshot đó. Test assert isinstance(result, VideoStatsSnapshot).

## Bài làm

Nhánh `fix/fix_core_store_return_type` — sửa lại sau lần CI fail, chỉ nộp 2 file theo quy tắc nhánh `fix/*`.

Nguyên nhân 1 failed: **lỗi ở fixture test, không phải ở code fix**. Sau `fix_idempotency_hourly_bucket` đã merge, `UnifiedVideoMetric.idempotency_key` tính theo `hour_bucket` (từ `collected_at`), không phải chỉ `date_bucket` — bằng chứng trong log CI: cả hai metric đều có `hour_bucket='2026-11-22T10:00'`. Metric thứ hai của tôi chỉ đổi `date_bucket` sang 2026-11-23 nhưng giữ nguyên `collected_at` 10:00 nên bị dedup đúng luật, `get_latest()` vẫn là 100. Stub contracts trên VM của tôi trước đây tính idempotency theo ngày nên không bắt được lỗi — đã sửa stub cho khớp logic theo giờ rồi chạy lại.

Sửa test: metric thứ hai đổi cả `collected_at` sang 2026-11-23 10:00 để có snapshot thật sự khác giờ, và thêm test hồi quy `test_same_hour_bucket_is_deduped_even_if_date_differs` chứng minh cùng giờ thì dedup dù khác ngày. Code `src/core/models.py` không đổi so với bản đã nộp trước (vẫn trả `VideoStatsSnapshot`).

Kiểm thử trên VM Linux (Python 3.12.3, pytest 9.2.0), với stub idempotency đã khớp bản thật theo giờ: `pytest tests/test_fix_core_store_return_type.py` = **6 passed** (5 cũ + 1 hồi quy mới).

(đã nộp file `src/core/models.py`)

(đã nộp file `tests/test_fix_core_store_return_type.py`)

## File đã nộp

- `src/core/models.py`
- `tests/test_fix_core_store_return_type.py`
