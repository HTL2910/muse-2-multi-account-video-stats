# Sửa lỗi `fix/fix_scheduler_compose_no_account_source`: Nối scheduler trong compose với nguồn account dùng chung thay vì no-op

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_scheduler_compose_no_account_source (fullstack-ux; systems-arch, algo-ml, backend-sec đồng ý). src/scheduler/__main__.py lấy account từ file JSON qua env SCHEDULER_ACCOUNTS_FILE và tự ghi 'không có file thì tick là logged no-op'; service scheduler trong compose không set biến đó cũng không mount file nào; account thật sống trong store in-memory của tiến trình API mà tiến trình scheduler không thấy được — kết quả 'docker compose up' cho scheduler khỏe mạnh nhưng không bao giờ lập lịch job nào, chức năng trung tâm chết lặng lẽ. Gốc rễ sâu hơn compose (backend-sec): API và scheduler là hai tiến trình với hai store in-memory tách biệt thì nối bằng file JSON chỉ là vá tạm. Fix: trước hết grep compose xác nhận SCHEDULER_ACCOUNTS_FILE không được set (algo-ml); hướng đúng là nối scheduler với nguồn account thật dùng chung — đọc accounts từ PostgreSQL khi có DATABASE_URL (service đã có env này) hoặc qua Redis như thiết kế scheduler đã dự phòng — và hạ cơ chế file JSON xuống vai trò dev-only có ghi rõ. Stopgap tối thiểu (compose set SCHEDULER_ACCOUNTS_FILE từ file API ghi ra) chỉ chấp nhận tạm thời, không tính là đóng lỗi. Tiêu chí đóng lỗi: tạo account qua API, chờ một tick, thấy job được enqueue (kèm test tích hợp up stack theo kịch bản đó). Không chặn CI, xếp sau các mục đỏ và các merge lại.

## Bài làm

Đã sửa và tự kiểm chứng trên VM: compose mới parse YAML sạch (đủ 8 service, thêm volume `scheduler_accounts`), **3 test passed**; negative control — gỡ phần wiring khỏi compose (trạng thái main cũ) → đúng test wiring fail, 2 test hành vi vẫn xanh (entrypoint vốn không hỏng, hỏng là ở tầng nối).

Thay đổi trong `docker/docker-compose.yml` (phần còn lại giữ nguyên từng dòng so với main):
- Thêm volume dùng chung **`scheduler_accounts`**, mount vào **cả api** (bên sở hữu account, ghi `accounts.json`) **và scheduler** (bên đọc) tại `/run/accounts`.
- Scheduler nhận **`SCHEDULER_ACCOUNTS_FILE: /run/accounts/accounts.json`** — trước đây biến này hoàn toàn vắng mặt (đã grep xác nhận như algo-ml yêu cầu) nên mọi tick là no-op im lặng dù `DATABASE_URL`/`REDIS_URL` có mặt (entrypoint không dùng hai env đó để đọc account — tôi đã đọc lại `src/scheduler/__main__.py` trên main để chắc chắn: nó CHỈ đọc file JSON, và chỉ nạp **một lần lúc khởi động**).
- Scheduler `depends_on` thêm `api: service_healthy` để file kịp tồn tại ở lần nạp đầu.
- Header compose ghi rõ cơ chế + giới hạn: sau khi api cập nhật account cần `docker compose restart scheduler` để nạp lại.

Test mới khóa cả hai tầng: (1) wiring trong compose (env + volume hai phía + khai báo volume + chờ api), có fallback kiểm tra bằng text khi môi trường thiếu PyYAML; (2) hành vi tích hợp đúng tiêu chí hội đồng ở mức entrypoint — chạy thật `python -m src.scheduler --once` với file account trên kênh dùng chung: account ACTIVE + 1 video → tick `enqueued >= 1, errors = 0`; và khi không có file thì vẫn là no-op có log, exit 0, không bịa job.

⚠ Phần sâu còn lại, đúng như backend-sec đã chỉ ra và nằm ngoài phạm vi nộp của nhánh này (chỉ compose + test riêng), tôi ghi rõ cả trong header compose lẫn docstring test để không ai tưởng lỗi đã đóng hẳn: entrypoint cần đọc account từ **store dùng chung (Postgres qua `DATABASE_URL` — env đã cấp sẵn cho service) ở mỗi tick**, hạ file JSON xuống dev-only, và api cần lưu account xuống store đó thay vì in-memory. Đó là việc của `src/scheduler/__main__.py` (file tôi viết ở vòng companions) + store của api, cần một nhánh riêng; phần compose này là cầu nối ở tầng stack và là điều kiện để nửa còn lại có chỗ cắm vào.

(đã nộp file `docker/docker-compose.yml`)

(đã nộp file `tests/test_fix_scheduler_compose_no_account_source.py`)

## File đã nộp

- `docker/docker-compose.yml`
- `tests/test_fix_scheduler_compose_no_account_source.py`
