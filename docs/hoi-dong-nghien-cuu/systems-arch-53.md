# Sửa lỗi `fix/fix_import_dual_contracts`: Khóa một kiểu import contracts duy nhất, xóa mọi fallback

_systems-arch · Chuyên gia Kiến trúc Hệ thống · lần nộp 1_

## Việc được giao

Gộp fix_import_dual_contracts + fix_risk_fallback_import (muse) và phần import của fix_audit_chain_thread_safety. Khóa duy nhất 'from src.contracts import ...' khi chạy từ repo root cho toàn repo; xóa toàn bộ fallback 'from contracts import' và 'from ..contracts import' (account_model, vault, risk, api/main, facebook_connector và mọi module khác). Sửa fallback sai 'from account_model import AuditLog, transition' trong src/risk/account_health.py bằng cách xóa fallback theo kiểu chuẩn này. Thêm tests/conftest.py và cấu hình pyproject đặt repo root lên sys.path. rbac_audit.py giữ nguyên 'from src.contracts import' (đó là kiểu đúng, không thêm fallback riêng). Test: smoke test import mọi module từ repo root và assert chỉ có một module object src.contracts.

## Bài làm

Nhánh `fix/fix_import_dual_contracts` — chỉ nộp 2 file theo quy tắc nhánh `fix/*`. Thay đổi duy nhất trong `account_model.py`: xóa khối `try/except ImportError` fallback `from contracts import`, giữ đúng một kiểu `from src.contracts import ...`, phần còn lại giữ nguyên từng dòng của bản trên `main`.

Lưu ý phạm vi: `tests/conftest.py`, cấu hình `pyproject.toml` và các module khác (`vault`, `risk/account_health.py`, `api/main.py`, `facebook_connector`, …) không thuộc file được giao trong nhánh này nên không nộp ở đây; test kèm theo tự đưa repo root lên `sys.path` nên không phụ thuộc conftest/pyproject. Sửa fallback sai `from account_model import ...` trong `risk/account_health.py` phải làm ở file đó, không thể nộp chung trong nhánh này.

Kiểm thử đã chạy trên VM Linux (Python 3.12.3, pytest 9.2.0) trong bản sao sạch: `pytest tests/test_fix_import_dual_contracts.py -v` = **5 passed**. Vì `src/contracts.py` thật trên `main` không có trên VM này, bản kiểm thử dùng stub contracts trung thực tại chỗ (chỉ để kiểm thử, không nộp) với đúng enum/dataclass/chữ ký và `ALLOWED_ACCOUNT_TRANSITIONS` từ contract đã khóa; kết quả thật trên `main` do CI báo cáo.

(đã nộp file `src/accounts/account_model.py`)

(đã nộp file `tests/test_fix_import_dual_contracts.py`)

## File đã nộp

- `src/accounts/account_model.py`
- `tests/test_fix_import_dual_contracts.py`
