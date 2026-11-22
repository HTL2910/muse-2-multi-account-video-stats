# Sửa lỗi `fix/fix_idempotency_hourly_bucket`: Khóa lại contract: idempotency theo giờ cho snapshot, theo ngày chỉ cho rollup

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp fix_daily_idempotency_blocks_intraday (algo-ml) + fix_idempotency_key_daily_vs_intraday (fullstack-ux) + phần lớn fix_collect_job_idempotency_collision (systems-arch). Đây là thay đổi contract do Team Lead khóa, bump CONTRACTS_VERSION lên 1.1.0: tách snapshot_key = hash(account_id, platform_video_id, bucket_giờ) cho time-series khỏi daily_key chỉ dùng cho rollup ngày; CollectJob idempotency key thêm bucket của scheduled_at. Sửa đồng bộ src/contracts.py, cả 3 MetricsStore (core, ingestion, time_series), IngestionPipeline và TIMESCALE_DDL trong cùng một PR. Test: 2 snapshot cùng ngày khác giờ đều được lưu, history có 2 điểm và velocity 1h > 0; job dedup phải trả job gốc kèm cờ deduplicated thay vì PENDING giả.

## Bài làm

Đã chạy kiểm thử trên máy ảo Linux (venv pytest 9.2.0, Python 3.12.3): **27 passed** — 10 test mới `test_fix_idempotency_hourly_bucket.py` + 17 test cũ `test_algo_ml_m2.py`, không regression.

Thay đổi chính trong `src/contracts.py` v1.1.0:
- `UnifiedVideoMetric.snapshot_key` theo giờ (`YYYY-MM-DDTHH:00` UTC), `idempotency_key` từ 1.1.0 chính là `snapshot_key`; `daily_key` chỉ dùng cho rollup ngày. Các store dedup theo `.idempotency_key` tự động thành theo giờ mà không cần sửa code của họ.
- `build_snapshot_idempotency_key` / `build_daily_idempotency_key` mới; `build_idempotency_key` giữ nguyên khóa ngày để tương thích ngược.
- `CollectJob` tự sinh key gồm bucket **phút** của `scheduled_at` (UTC, `YYYY-MM-DDTHH:MM`) để job FETCH_STATS 15 phút/lần không bị dedup lẫn nhau; thêm `deduplicated` / `duplicate_of` để scheduler trả job gốc kèm cờ dedup thay vì PENDING giả.
- Sửa thêm 1 dòng trong `AccountHealth.computed_at`: `field(default_factory=utcnow())` gọi hàm khi `utcnow` chưa được định nghĩa ở cuối file nên module không import được; đã sửa thành `lambda: utcnow()` như mọi dataclass khác.
- Chỉ nộp 2 file được giao, không chạm các file domain khác. `TIMESCALE_DDL` (UNIQUE `(time, snapshot_key)`) thuộc `src/analytics/time_series.py`, cần PR tiếp theo theo ghi chú trong contract.

(đã nộp file `src/contracts.py`)

(đã nộp file `tests/test_fix_idempotency_hourly_bucket.py`)

## File đã nộp

- `src/contracts.py`
- `tests/test_fix_idempotency_hourly_bucket.py`
