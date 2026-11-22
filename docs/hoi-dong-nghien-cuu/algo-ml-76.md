# Sửa lỗi `fix/fix_timeseries_rollup_ddl`: Rollup invalidate cache khi ghi mới và DDL TimescaleDB chứa cột time

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp fix_timeseries_rollup_cache_stale (algo-ml) + fix_timescale_unique_key_missing_partiti (backend-sec). rollup() cache 300 giây theo (video_id, bucket_hours) nhưng save_snapshot() không invalidate nên dashboard thấy số cũ tới 5 phút; TIMESCALE_DDL khai UNIQUE(idempotency_key) rồi create_hypertable('video_metrics','time') sẽ lỗi vì unique index trên hypertable phải chứa cột phân vùng time, và test in-memory không bao giờ chạy DDL. Fix: invalidate key rollup:{video_id}:* trong save_snapshot() (hoặc không cache rollup khi có ghi mới); đổi ràng buộc thành UNIQUE(time, idempotency_key) kết hợp với snapshot_key theo giờ ở fix_idempotency_hourly_bucket, hoặc bỏ unique khỏi hypertable và dedupe bằng bảng idempotency_keys riêng. Chạy thử DDL trên TimescaleDB thật trong docker-compose và thêm migration test vào CI. Test: lưu snapshot rồi rollup phải thấy ngay giá trị mới.

## Bài làm

Nhánh `fix/fix_timeseries_rollup_ddl` — algo-ml đã tái hiện cả 2 lỗi trên VM trước khi sửa, rồi chạy test PASS.

**Tái hiện (trước fix):** lưu snapshot 100 views ngày 20/11, rollup → `[100]`; lưu tiếp 300 views ngày 21/11, rollup vẫn chỉ `[100]` (stale tới 5 phút). DDL có `UNIQUE (idempotency_key)` trên hypertable.

**Fix 1 — cache stale:** `_TTLCache` thêm `delete()` / `invalidate_prefix()`; `TimeSeriesStore.save_snapshot()` sau khi ghi snapshot mới gọi `invalidate_rollup(video_id)` — xóa mọi rollup đã cache của video đó (mọi `bucket_hours`), rollup kế tiếp tính lại từ dữ liệu mới. Snapshot duplicate (cùng key theo giờ) không ghi mới nên không invalidate; invalidate chỉ giới hạn trong 1 video. Cache vẫn hoạt động khi không có ghi mới.

**Fix 2 — DDL:** bỏ UNIQUE khỏi hypertable `video_metrics` (unique index trên hypertable bắt buộc chứa cột phân vùng `time`, và ngay cả `UNIQUE (time, idempotency_key)` cũng không enforce dedup theo giờ vì 2 snapshot cùng giờ có `time` chính xác khác nhau). Dedup theo giờ chuyển sang bảng thường `idempotency_keys` với `PRIMARY KEY (snapshot_key)` — write path: `INSERT ... ON CONFLICT (snapshot_key) DO NOTHING`, chỉ insert metric khi key mới. Thêm cột `snapshot_key` + index vào `video_metrics`.

**Lưu ý trung thực về kiểm chứng DDL:** VM này không có docker/psql nên không chạy được TimescaleDB thật tại đây; test kiểm chứng DDL bằng phân tích tĩnh + mô phỏng semantics dedup bằng sqlite3 (stdlib). Bước chạy DDL trên TimescaleDB thật trong docker-compose là bước CI, không claim đã chạy.

**Kết quả test trên VM (Python 3.12.3, pytest 9.2.0): 81 passed** = 10 test mới + 17 algo-ml m2 + 10 idempotency + 15 TikTok auth + 12 proxy pool + 17 ingestion. Chỉ nộp 2 file theo quy tắc nhánh fix.

(đã nộp file `src/analytics/time_series.py`)

(đã nộp file `tests/test_fix_timeseries_rollup_ddl.py`)

## File đã nộp

- `src/analytics/time_series.py`
- `tests/test_fix_timeseries_rollup_ddl.py`
