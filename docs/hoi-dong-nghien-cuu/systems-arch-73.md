# Sửa lỗi `fix/fix_docker_compose_unrunnable`: Làm docker compose chạy được: đủ Dockerfile, scheduler entrypoint, healthcheck đúng

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_docker_compose_unrunnable (fullstack-ux). Compose tham chiếu docker/Dockerfile.api, docker/Dockerfile.worker, docker/Dockerfile.frontend nhưng docker/ không có Dockerfile nào; scheduler chạy 'python -m src.scheduler.task_scheduler --beat' nhưng task_scheduler.py không có khối __main__, không parse argv, không có vòng lặp beat nên container chết ngay và restart loop; api healthcheck probe /healthz trong khi FastAPI chỉ có GET /health; secrets trỏ tới ../.secrets/*.txt nhưng .gitignore không chứa .secrets/ và repo không có script/tài liệu tạo file này. Fix: commit đủ Dockerfile.api/worker/frontend (multi-stage, non-root, chỉ copy src cần thiết); thêm entrypoint scheduler thật (ví dụ src/scheduler/__main__.py có vòng lặp tick gọi schedule_jobs_for_account + enqueue, xử lý SIGTERM graceful) hoặc đổi command sang entrypoint đó; sửa healthcheck thành /health; thêm .secrets/ và .env vào .gitignore, cung cấp .secrets/README hoặc scripts/init_secrets.sh sinh file mẫu ở local; CI chạy 'docker compose config + build' và test bằng docker compose up.

## Bài làm

Nhánh `fix/fix_docker_compose_unrunnable` — chỉ nộp 2 file theo quy tắc nhánh `fix/*`.

Đã xác minh trước khi sửa (đọc bản `main`): FastAPI chỉ có `GET /health`, không có `/healthz`; `task_scheduler.py` không có khối `__main__`, không parse argv, không có vòng lặp beat; `.gitignore` không chứa `.secrets/` hay `.env`.

Những gì sửa được **trong** `docker/docker-compose.yml`:
- **Healthcheck api**: probe endpoint `/health` thật của FastAPI thay cho endpoint cũ không tồn tại (khiến api mãi không healthy, các service phụ thuộc treo ở `service_healthy`).
- **Scheduler**: command đổi sang `python -m src.scheduler --beat` (entrypoint thật), thêm env `SCHEDULER_INTERVAL_SECONDS` (mặc định 60s); giữ `stop_grace_period: 30s` và đúng 1 replica.
- **Backup DB**: `pg_dump` không đọc password từ biến môi trường dạng `*_FILE` (chỉ entrypoint của image postgres mới đọc) nên entrypoint giờ `export PGPASSWORD=$(cat /run/secrets/postgres_password)` trước khi dump.
- Giữ nguyên: 3 Dockerfiles được tham chiếu, secrets vẫn file-based ở `../.secrets/*.txt`, volumes tách riêng DB/vault, `depends_on` với `service_healthy`.

Những gì **không thể nộp trong nhánh này** và được ghi rõ trong header compose để bước merge không quên (nếu thiếu thì `up` vẫn fail — đó là lỗi thiếu file đi kèm, không phải lỗi compose): `docker/Dockerfile.api` (CMD uvicorn), `docker/Dockerfile.worker` (CMD vòng lặp worker), `docker/Dockerfile.frontend` (build `src/web`), `src/scheduler/__main__.py` (argparse `--beat`, vòng lặp tick gọi `schedule_jobs_for_account()` + `enqueue()`, xử lý SIGTERM), `scripts/init_secrets.sh` (tạo `../.secrets/*.txt`, chmod 600), và `.gitignore` thêm `.secrets/` + `.env`. Quy trình bring-up (bước 0 chạy init script rồi mới `up`) cũng ghi trong header.

Kiểm thử trên VM Linux (Python 3.12.3, pytest 9.2.0, pyyaml 6.0.4): `pytest tests/test_fix_docker_compose_unrunnable.py` = **9 passed, 1 skipped** (test `docker compose config` skip vì VM này không có docker; test đó tự tạo secret giả tạm thời rồi dọn sạch). Kết quả thật trên `main` do CI báo cáo.

(đã nộp file `docker/docker-compose.yml`)

(đã nộp file `tests/test_fix_docker_compose_unrunnable.py`)

## File đã nộp

- `docker/docker-compose.yml`
- `tests/test_fix_docker_compose_unrunnable.py`
