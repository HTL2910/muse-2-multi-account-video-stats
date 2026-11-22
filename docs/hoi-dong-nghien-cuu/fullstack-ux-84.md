# Sửa lỗi `fix/fix_contracts_version_stale_tests`: Cập nhật test khóa CONTRACTS_VERSION lên 1.1.0 đã khóa

_fullstack-ux · Chuyên gia Ứng dụng & Tích hợp · lần nộp 1_

## Việc được giao

Gộp 5 báo cáo trùng (muse fix_test_version_assert_stale_1_1_0, systems-arch fix_contracts_version_stale_tests, algo-ml fix_contracts_version_lock_stale, backend-sec fix_contracts_version_lock_tests_stale, fullstack-ux fix_contracts_version_pin_stale). Lỗi ở TEST, không ở code: bump 1.0.0 -> 1.1.0 là thay đổi Team Lead đã khóa (snapshot_key theo giờ, CollectJob key theo phút); tuyệt đối không hạ contracts về 1.0.0. Đổi assert CONTRACTS_VERSION == '1.0.0' thành '1.1.0' ở cả tests/test_fullstack_ux_m2.py::test_contracts_are_real_and_locked và tests/test_fix_test_portability_stub_contracts.py::test_contracts_module_is_the_real_repo_file; sửa docstring 'v1.0.0' thành 'v1.1.0' ở hai file test và src/accounts/account_model.py. Đặt EXPECTED_CONTRACTS_VERSION ở một chỗ duy nhất (hằng trong tests/conftest.py hoặc đọc từ changelog trong contracts.py) để lần bump sau chỉ sửa một nơi. Quy ước mới: mọi PR bump CONTRACTS_VERSION phải sửa đồng thời các test khóa phiên bản trong cùng PR. Test: pytest -q hết 2 lỗi đỏ này.

## Bài làm

(đã nộp file `tests/test_fullstack_ux_m2.py`)

(đã nộp file `tests/test_fix_contracts_version_stale_tests.py`)

## File đã nộp

- `tests/test_fullstack_ux_m2.py`
- `tests/test_fix_contracts_version_stale_tests.py`
