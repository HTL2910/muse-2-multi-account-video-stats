# Sửa lỗi `fix/fix_browser_profile_geo_runtime`: Browser profile lấy geo từ proxy sticky và giữ runtime qua close/mở lại

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 2_

## Việc được giao

Gộp fix_browser_profile_geo_always_us (algo-ml) + fix_profile_geo_always_us + fix_profile_close_loses_runtime (backend-sec). Biểu thức country = str(getattr(account, 'niche', '') and '' or '') luôn ra '' nên mọi profile đều America/New_York/en-US dù proxy ở đâu; close() pop _runtime nhưng giữ _profiles/_by_account nên lần mở lại mất theo dõi pid/temp và dễ Chromium zombie; manual_queue chỉ là list RAM, restart là mất. Fix: lấy country từ ProxyInfo của proxy sticky đang bind (qua ProxyBinding/proxy_id), không suy từ niche; không xác định được country thì fail-closed hoặc UTC + cảnh báo, không mặc định US; xóa dòng fp_key thừa; close() hoặc xóa hẳn profile khỏi cả 3 dict hoặc get_or_create nhánh existing phải tạo lại _runtime nếu thiếu; manual_queue chuyển sang hàng đợi bền vững (DB/queue của scheduler) có trạng thái và retry. Test: proxy VN -> Asia/Ho_Chi_Minh/vi-VN, proxy JP -> Asia/Tokyo/ja-JP, fingerprint giữ nguyên giữa hai lần get_or_create, đóng-mở nhiều vòng pid/temp được dọn sạch.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_browser_profile_geo_runtime` — NỘP LẠI (sửa lỗi test fail trên Windows)**

**Chẩn đoán lỗi:** lần chạy trước fail đúng 1 test (`test_close_kills_registered_process`) và lỗi nằm ở **môi trường Windows của máy chạy test**, không phải logic fix: `os.killpg`/`os.getpgid` là API POSIX, **không tồn tại trên Windows**, nên lời gọi thẳng trong `close()` ném `AttributeError` — mà khối `except` cũ chỉ bắt `ProcessLookupError/PermissionError/OSError` nên không nuốt được. (Đây cũng là lỗi tiềm ẩn có sẵn trong code gốc trên `main`; test mới của tôi là test đầu tiên thực sự kill một tiến trình thật nên mới lộ ra.) Trên Linux VM của tôi test này pass vì `killpg` tồn tại.

**Cách sửa (chỉ trong `src/browser/profile_manager.py`, phần còn lại giữ nguyên bản nộp trước):** tách việc kết thúc tiến trình thành helper `_terminate_process(pid)` đa nền tảng và `close()` chỉ gọi helper này:
- POSIX: vẫn `killpg(getpgid, SIGTERM)` như cũ, nhưng tra cứu qua `getattr(os, "killpg", None)` và bắt thêm `AttributeError` — nền tảng thiếu API này thì rơi xuống `os.kill(pid, SIGTERM)`.
- Windows (`os.name == "nt"`): dùng `taskkill /PID <pid> /T /F` (kill cả cây tiến trình con, đúng mục đích chống Chromium zombie), dự phòng `os.kill`.
- Mọi lỗi kết thúc (tiến trình đã chết, thiếu quyền, pid không hợp lệ) đều được nuốt — `close()` không bao giờ được phép crash vì một pid cũ.

**Kiểm chứng:** trên VM Linux, `pytest tests/test_fix_browser_profile_geo_runtime.py -q` → **12 passed**. Vì máy chạy test là Windows, tôi mô phỏng thêm đúng điều kiện lỗi cũ ngay trên VM: xóa `os.killpg` khỏi module `os` rồi gọi `_terminate_process` — không còn `AttributeError`, tiến trình ngủ 60s vẫn bị kill qua đường dự phòng, và pid đã chết được nuốt êm. File test giữ nguyên như bản nộp trước (test đúng, code sai).

(đã nộp file `src/browser/profile_manager.py`)

(đã nộp file `tests/test_fix_browser_profile_geo_runtime.py`)

## File đã nộp

- `src/browser/profile_manager.py`
- `tests/test_fix_browser_profile_geo_runtime.py`
