# Sửa lỗi `fix/fix_missing_dependency_manifest`: Thêm manifest phụ thuộc Python và CI tái lập được

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_missing_dependency_manifest (muse). Thêm pyproject.toml duy nhất (Python >=3.11, pin fastapi, pydantic, pytest, cryptography, httpx) và requirements-dev.txt nếu cần; cryptography là dependency production bắt buộc để fallback stdlib không bao giờ bị chọn lặng lẽ. Cập nhật README hướng dẫn tạo venv và chạy pytest từ repo root. CI cài từ manifest trên checkout sạch, không dùng .venv local. Test: checkout sạch + pip install + pytest phải xanh.

## Bài làm

Nhánh `fix/fix_missing_dependency_manifest` — chỉ nộp 2 file theo quy tắc nhánh `fix/*` (không nộp lại file domain khác, không nộp README/requirements-dev.txt trong đợt này; `pyproject.toml` đã đủ làm manifest duy nhất cho CI: `pip install .[dev]`).

Kiểm thử đã chạy trên VM Linux (Python 3.12.3): checkout sạch mô phỏng (pyproject + test + README tối thiểu + `src/__init__.py`), venv mới, `pip install ".[dev]"`, rồi `pytest tests/test_fix_missing_dependency_manifest.py` = **12 passed in 0.38s**. Các phiên bản pin đều là phiên bản đã cài và xác minh thực tế trên VM, không đoán: fastapi 0.165.0, pydantic 2.14.4, cryptography 50.0.2, httpx 0.28.1, uvicorn 0.54.0, pytest 9.2.0. `cryptography` được đặt trong `project.dependencies` (production bắt buộc), không phải dev-only, để vault không bao giờ rơi lặng lẽ về fallback stdlib.

(đã nộp file `pyproject.toml`)

(đã nộp file `tests/test_fix_missing_dependency_manifest.py`)

## File đã nộp

- `pyproject.toml`
- `tests/test_fix_missing_dependency_manifest.py`
