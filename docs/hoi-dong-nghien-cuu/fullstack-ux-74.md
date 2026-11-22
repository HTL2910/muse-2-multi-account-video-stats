# Sửa lỗi `fix/fix_missing_tests_critical_modules`: Bổ sung test cho vault, YouTube, browser, alert và collect_sample_data

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp fix_missing_tests_critical_modules (muse) + fix_no_tests_for_backend_sec_approaches (backend-sec). tests/ chỉ có 3 file, không file nào import src.accounts.vault, src.connectors.youtube_connector, src.browser.profile_manager, src.api.alert_rule_engine hay scripts/collect_sample_data nên 35 passed không bao phủ các đường code chứa secret và fail-closed quan trọng nhất. Fix: thêm tests/test_backend_sec_m2.py tối thiểu — vault store/rotate/restart-cùng-KEK/tamper-detection, token refresh race + revoked -> NEEDS_REAUTH, YouTube quota/batch/analytics mapping, profile fingerprint ổn định + lệch proxy thì fail-closed + checkpoint vào manual queue, alert dedupe/cooldown/sync-fail-3-lần, collect_sample_data chạy sinh đủ golden cases và không lưu credential thật. CI phải báo cáo coverage theo module.

## Bài làm

(đã nộp file `tests/test_backend_sec_m2.py`)

(đã nộp file `tests/test_fix_missing_tests_critical_modules.py`)

## File đã nộp

- `tests/test_backend_sec_m2.py`
- `tests/test_fix_missing_tests_critical_modules.py`
