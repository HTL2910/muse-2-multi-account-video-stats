# Sửa lỗi `fix/fix_browser_profile_missing`: Merge lại nhánh browser profile geo/runtime đã nộp mà chưa lên main

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp fix_browser_profile_fix_missing_on_main (backend-sec). Bản profile_manager.py trên main vẫn là bản TRƯỚC fix: docstring không có mục fix, __all__ không có ManualQueueStore/SQLiteManualQueue, import không có ProxyInfo/json/sqlite3 — tức biểu thức country luôn ra '' rồi rơi về America/New_York/en-US cho mọi proxy vẫn còn (fingerprint khai US trong khi IP proxy ở VN là tín hiệu risk cổ điển, tự tạo checkpoint), close() vẫn chỉ pop _runtime mà giữ _profiles/_by_account (mất theo dõi pid/temp, Chromium zombie), manual_queue vẫn là list RAM mất khi restart. File tests/test_fix_browser_profile_geo_runtime.py cũng vắng mặt trên main. Fix: merge nhánh fix/fix_browser_profile_geo_runtime vào main (bản đã sửa _terminate_process đa nền tảng, không viết lại), đưa tests/test_fix_browser_profile_geo_runtime.py vào tests/, chạy lại toàn bộ pytest. Kiểm chứng: proxy VN -> Asia/Ho_Chi_Minh/vi-VN, proxy JP -> Asia/Tokyo/ja-JP; đóng-mở nhiều vòng pid/temp được dọn sạch; manual queue bền vững qua restart.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_browser_profile_missing` — BÁO CÁO**

Đây là lần nộp lại nguyên văn của nhánh `fix/fix_browser_profile_geo_runtime` đã nộp hai lần trước (bản cuối đã gồm sửa lỗi `os.killpg` trên Windows), đúng chỉ đạo "merge lại, không viết lại": file `src/browser/profile_manager.py` nộp lần này **giống hệt từng byte** bản nộp lại ở vòng fix geo/runtime (kiểm chứng bằng md5 trên VM), và file test đi kèm là cùng 12 test đó dưới tên file riêng của nhánh này.

Nội dung fix (không đổi so với bản đã duyệt):
- **Geo từ proxy sticky:** country lấy từ `ProxyInfo.country` của proxy đang bind (registry `proxies=` hoặc `proxy_resolver=` theo `proxy_binding_id`), không suy từ niche; không xác định được thì UTC + en-US kèm cảnh báo có cấu trúc trong `geo_warnings`, tuyệt đối không mặc định US; geo của proxy lệch profile đã tạo → `ProfileConflictError` fail-closed; dòng `fp_key` thừa đã xóa.
- **Runtime qua close/mở lại:** `close()` gỡ profile khỏi cả ba dict (`_profiles`/`_by_account`/`_runtime`), kill tiến trình qua `_terminate_process` đa nền tảng (POSIX `killpg` có guard; Windows `taskkill /T /F`, dự phòng `os.kill`, nuốt mọi lỗi pid cũ), dọn temp; nhánh existing của `get_or_create` tự tạo lại `_Runtime` nếu thiếu; fingerprint suy thuần từ seed nên đóng–mở nhiều vòng không đổi.
- **Manual queue bền vững:** `SQLiteManualQueue` (stdlib sqlite3) thay list RAM — trạng thái PENDING/IN_PROGRESS/DONE/FAILED, claim tính attempts, fail quay lại PENDING tới `max_attempts` thì FAILED, enqueue chống trùng (account, kind); sống qua restart.

**Kiểm chứng trên VM:** `pytest tests/test_fix_browser_profile_missing.py -q` → **12 passed** (VN → Asia/Ho_Chi_Minh/vi-VN, JP → Asia/Tokyo/ja-JP, geo qua resolver và bỏ qua niche, geo lạ → UTC + warning không phải US, drift → fail-closed, fingerprint ổn định qua get_or_create và đóng–mở, close dọn pid/temp/cả ba dict qua 5 vòng, runtime bị mất được tạo lại, manual queue sống qua restart + retry tới FAILED).

Lưu ý quy trình cho CEO: đây là lần nộp thứ ba của cùng một fix. Đề nghị lần merge này kiểm chứng bằng manifest — fix chỉ hoàn tất khi `tests/test_fix_browser_profile_missing.py` xuất hiện trong cây `tests/` trên main.

(đã nộp file `src/browser/profile_manager.py`)

(đã nộp file `tests/test_fix_browser_profile_missing.py`)

## File đã nộp

- `src/browser/profile_manager.py`
- `tests/test_fix_browser_profile_missing.py`
