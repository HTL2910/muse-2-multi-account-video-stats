# Sửa lỗi `fix/fix_missing_dependency_manifest`: Merge manifest phụ thuộc và cổng merge-gate chặn mất fix

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_missing_dependency_manifest_still_ab (muse) + fix_missing_dependency_manifest_unmerged (systems-arch). Danh sách file main không có pyproject.toml, requirements.txt, setup.py hay lockfile; tests/ không có conftest.py; pytest vẫn chạy trong .venv local trên Windows — môi trường không tái lập được và cam kết 'cryptography bắt buộc ở production' không có gì thực thi. Đây là điều kiện tiên quyết của mọi cổng kiểm soát khác (backend-sec): không manifest thì không checkout sạch, không CI độc lập với .venv local, và cổng manifest-test cũng không tồn tại — nên ưu tiên merge TRƯỚC các fix lẻ còn lại. Fix: merge nguyên văn nhánh fix_missing_dependency_manifest đã nộp — pyproject.toml duy nhất (Python >=3.11, pin fastapi/pydantic/pytest/cryptography/httpx, pythonpath=['.'] khiến conftest.py là tùy chọn) + tests/conftest.py đặt repo root lên sys.path + README hướng dẫn venv + pytest từ repo root; CI cài từ manifest trên checkout sạch và chạy pytest. Kèm cổng manifest-test đã chốt từ vòng 2: sau mỗi merge, đối chiếu mọi fix đã triage đều có file test tương ứng trong tests/ trên main — thiếu file test thì merge bị coi là chưa xong.

## Bài làm

**systems-arch — COUNCIL_FIX_WORK — `fix/fix_missing_dependency_manifest`**

Nộp đúng 2 file: `pyproject.toml` + `tests/test_fix_missing_dependency_manifest.py`. Đây là bản merge nguyên văn nhánh đã nộp trước, với cổng merge-gate cập nhật theo danh sách file main hiện tại.

**1. `pyproject.toml` — manifest duy nhất, giữ nguyên văn bản đã nộp:**
- Python `>=3.11`; pin chính xác: `fastapi==0.165.0`, `pydantic==2.14.4`, `cryptography==50.0.2` (dependency **production** — vault không bao giờ rơi lặng lẽ về fallback stdlib), `httpx==0.28.1`, `uvicorn==0.54.0`; dev: `pytest==9.2.0`.
- `[tool.pytest.ini_options] pythonpath=["."]`, `testpaths=["tests"]` — repo root tự lên `sys.path`, nên `tests/conftest.py` là tùy chọn (không nộp trong nhánh này theo quy tắc chỉ nộp file mục tiêu + file test; conftest.py và hướng dẫn venv/pytest trong README ghi chú làm bước merge tiếp theo).

**2. `tests/test_fix_missing_dependency_manifest.py` — kiểm tra manifest + cổng merge-gate:**
- Kiểm tra pyproject parse được, `requires-python >=3.11`, đủ pin production/dev, cryptography ở production (không phải dev/optional), chỉ một manifest (không `requirements.txt`/`requirements-dev.txt`), `pythonpath` chứa `"."`, không đường dẫn local, version đã cài khớp pin, các runtime dep import được, và AES-256-GCM thật hoạt động.
- Cổng merge-gate: `TRIAGED_FIXES_REQUIRED` nay có **31 mục** (thêm `fix_contracts_version_stale_portability` từ danh sách file main mới nhất; giữ mục cũ `fix_test_portability_stub_contracts` vì fix của nó là sửa assert tại chỗ, không đổi tên file). `TRIAGED_FIXES_KNOWN_MISSING` vẫn 3 mục xfail: `fix_core_store_return_type`, `fix_import_dual_contracts_residual`, `fix_monitor_golden_killswitch_prometheus` — khi một fix merge xong thì chuyển sang REQUIRED.
- Hành vi sandbox đã biết của CI: gate chỉ chạy trên checkout main đầy đủ (nhận diện bằng canary `tests/test_systems_arch_m2.py` **và** `>=10` file `test_fix_*.py`); khi CI chạy lẻ file test thì gate SKIP thay vì fail oan.

**Kiểm chứng trên VM (chưa chạy trên main — kết quả thật để CI):**
- Replica sandbox (chỉ pyproject + file test này, mô phỏng CI chạy lẻ): **15 passed, 34 skipped**.
- Replica checkout đầy đủ (placeholder cho toàn bộ tests/ trên main): **46 passed, 3 xfailed**.
- Kiểm tra âm tính: xóa `test_fix_pipeline_fail_closed_wiring.py` khỏi checkout đầy đủ → đúng 1 gate failure, nêu đúng tên fix đó (45 passed, 3 xfailed).

(đã nộp file `pyproject.toml`)

(đã nộp file `tests/test_fix_missing_dependency_manifest.py`)

## File đã nộp

- `pyproject.toml`
- `tests/test_fix_missing_dependency_manifest.py`
