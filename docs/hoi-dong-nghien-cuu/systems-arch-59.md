# Sửa lỗi `fix/fix_core_store_return_type`: Core InMemoryMetricsStore.save_snapshot trả VideoStatsSnapshot theo protocol

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_core_metrics_store_return_type (systems-arch, đã kiểm chứng VM) + fix_metrics_store_return_type_mismatch (algo-ml). Contracts MetricsStore.save_snapshot() khai báo trả VideoStatsSnapshot, ingestion/analytics trả đúng kiểu, nhưng core trả thẳng UnifiedVideoMetric nên code gọi qua protocol và truy cập .metric sẽ AttributeError và các store không thể thay thế nhau. Fix: trả VideoStatsSnapshot(id=new_id(), metric=metric, raw_payload_ref='') và lưu snapshot đó. Test assert isinstance(result, VideoStatsSnapshot).

## Bài làm

Nhánh `fix/fix_core_store_return_type` — chỉ nộp 2 file theo quy tắc nhánh `fix/*`. Thay đổi duy nhất trong `src/core/models.py`: `InMemoryMetricsStore.save_snapshot()` giờ trả `VideoStatsSnapshot(id=new_id(), metric=metric, raw_payload_ref="")` và lưu snapshot đó vào `_by_key`, thay vì trả thẳng `UnifiedVideoMetric`. `get_history()`/`get_latest()`/`daily_rollup()` vẫn trả `UnifiedVideoMetric` như cũ; tính idempotent theo `idempotency_key` không đổi. Phần còn lại giữ nguyên từng dòng của bản trên `main`, kể cả khối import try/except (thuộc phạm vi `fix_import_dual_contracts`, không sửa ở nhánh này).

Kiểm thử đã chạy trên VM Linux (Python 3.12.3, pytest 9.2.0): `pytest tests/test_fix_core_store_return_type.py -v` = **5 passed**. Vì `src/contracts.py` thật trên `main` không có trên VM này, bản kiểm thử dùng stub contracts trung thực tại chỗ (chỉ để kiểm thử, không nộp) với đúng `VideoStatsSnapshot(id, metric, raw_payload_ref)` và `UnifiedVideoMetric` theo phương án hội đồng đã thống nhất; kết quả thật trên `main` do CI báo cáo.

(đã nộp file `src/core/models.py`)

(đã nộp file `tests/test_fix_core_store_return_type.py`)

## File đã nộp

- `src/core/models.py`
- `tests/test_fix_core_store_return_type.py`
