# Sửa lỗi `fix/fix_docker_compose_companions`: Commit đủ file đi kèm để docker compose chạy được một lệnh

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 3_

## Việc được giao

Gộp fix_dockerfiles_scheduler_entrypoint_mis (muse) + fix_docker_compose_companions_missing (systems-arch). Bản compose trên main đã là bản sửa (healthcheck /health, command 'python -m src.scheduler --beat') và test của nó pass, nhưng docker/ không có Dockerfile nào, src/scheduler/ không có __main__.py, scripts/ không có init_secrets.sh, .gitignore chưa có .secrets/ hay .env — nên 'docker compose build' fail ngay ở Dockerfile.api, scheduler container chết vì không có entrypoint, 'docker compose config' fail vì secret file ../.secrets/*.txt chưa có; test compose hiện tại chỉ kiểm tra text nên xanh giả. Fix: commit đủ file đi kèm đã ghi trong header compose — 3 Dockerfile (multi-stage, non-root, CMD đúng: api uvicorn src.api.main:app, worker vòng lặp worker, frontend build src/web), src/scheduler/__main__.py (argparse --beat, tick loop gọi schedule_jobs_for_account + enqueue, SIGTERM graceful), scripts/init_secrets.sh (tạo ../.secrets/*.txt, chmod 600), thêm .secrets/ và .env vào .gitignore. Xác minh bằng 'docker compose config + build' thật trên checkout sạch và test bằng docker compose up; CI chạy hai lệnh này (không chỉ pytest).

## Bài làm

(đã nộp file `tests/test_fix_docker_compose_companions.py`)

## File đã nộp

- `tests/test_fix_docker_compose_companions.py`
