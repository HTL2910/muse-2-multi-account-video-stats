# Sửa lỗi `fix/fix_browser_profile_geo_runtime`: Browser profile lấy geo từ proxy sticky và giữ runtime qua close/mở lại

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp fix_browser_profile_geo_always_us (algo-ml) + fix_profile_geo_always_us + fix_profile_close_loses_runtime (backend-sec). Biểu thức country = str(getattr(account, 'niche', '') and '' or '') luôn ra '' nên mọi profile đều America/New_York/en-US dù proxy ở đâu; close() pop _runtime nhưng giữ _profiles/_by_account nên lần mở lại mất theo dõi pid/temp và dễ Chromium zombie; manual_queue chỉ là list RAM, restart là mất. Fix: lấy country từ ProxyInfo của proxy sticky đang bind (qua ProxyBinding/proxy_id), không suy từ niche; không xác định được country thì fail-closed hoặc UTC + cảnh báo, không mặc định US; xóa dòng fp_key thừa; close() hoặc xóa hẳn profile khỏi cả 3 dict hoặc get_or_create nhánh existing phải tạo lại _runtime nếu thiếu; manual_queue chuyển sang hàng đợi bền vững (DB/queue của scheduler) có trạng thái và retry. Test: proxy VN -> Asia/Ho_Chi_Minh/vi-VN, proxy JP -> Asia/Tokyo/ja-JP, fingerprint giữ nguyên giữa hai lần get_or_create, đóng-mở nhiều vòng pid/temp được dọn sạch.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_browser_profile_geo_runtime` (nhánh `fix/fix_browser_profile_geo_runtime`)**

Đã đọc bản mới nhất trên `main` trước khi sửa; chỉ nộp file mục tiêu + file test riêng, giữ nguyên `HumanBehaviour`, `detect_block_page`, `launch_args` và các hành vi hợp lệ khác. Tóm tắt bản vá:

- **Geo lấy từ proxy sticky thật:** biểu thức cũ `str(getattr(account, "niche", "") and "" or "")` — luôn cho ra `""` rồi rơi về mặc định `"US"` — đã bị xóa hẳn. Country nay được phân giải từ `ProxyInfo.country` của proxy sticky đang bind với account, qua ba nguồn rõ ràng: registry `proxies=` (key là `account.proxy_binding_id`), callable `proxy_resolver=` tra theo binding, hoặc `account.proxy` nếu account mang sẵn `ProxyInfo`. Không xác định được country thì dùng **UTC + en-US kèm cảnh báo có cấu trúc** (ghi vào `manager.geo_warnings` và log JSON event `profile_geo_unresolved`) — tuyệt đối không mặc định US nữa. Thêm chống drift: profile đã tạo mà country hiện tại của proxy cho timezone/locale khác với bản đã lưu → `ProfileConflictError` fail-closed. Dòng `fp_key` thừa trong `_fingerprint` đã xóa, chỉ còn một lần tính thuần từ seed.
- **close() dọn sạch cả ba dict:** trước đây `close()` chỉ pop `_runtime`, giữ `_profiles`/`_by_account` nên mở lại là mất theo dõi pid/temp và dễ để Chromium zombie. Nay `close()` gỡ profile khỏi **cả ba** dict, vẫn kill process group + dọn temp của runtime và thư mục `tmp` của profile như cũ, và idempotent (đóng id không tồn tại là no-op). Việc gỡ đăng ký an toàn vì profile được suy lại xác định từ `seed(account_id)` ở lần `get_or_create` sau — fingerprint không đổi. Nhánh existing của `get_or_create` cũng tự tạo lại `_Runtime` nếu thiếu (phòng trạng thái sót từ bản cũ), và có thêm `register_runtime()`/`runtime_for()` để worker ghi pid/temp cho `close()` dọn thay vì chọc thẳng vào dict nội bộ.
- **manual_queue bền vững:** list RAM cũ được thay bằng `SQLiteManualQueue` (stdlib sqlite3, mặc định file `manual_queue.sqlite3` cạnh `base_dir`, hoặc tiêm store/đường dẫn riêng — kể cả hàng đợi của scheduler nếu theo cùng giao diện `ManualQueueStore`). Mỗi mục có trạng thái `PENDING → IN_PROGRESS → DONE/FAILED`, `claim` tăng `attempts`, `fail` dưới ngưỡng thì quay lại `PENDING` để retry, quá `max_attempts` thì `FAILED`; enqueue trùng (account, kind) đang chờ thì trả về mục cũ, không nhân đôi. `manager.manual_queue` giờ là property đọc từ store, kèm `claim/complete/fail_manual_item`.

**Kiểm chứng trên VM Linux:** `pytest tests/test_fix_browser_profile_geo_runtime.py -q` → **12 passed**, đúng các ca spec yêu cầu: proxy VN → `Asia/Ho_Chi_Minh`/`vi-VN`, proxy JP → `Asia/Tokyo`/`ja-JP`, resolver + niche="VN" nhưng proxy KR vẫn ra Seoul (niche bị bỏ qua hoàn toàn), không có nguồn proxy → UTC + đúng 1 cảnh báo và không phải giờ US; fingerprint giống hệt nhau giữa hai lần `get_or_create` và sau đóng–mở lại; đóng–mở 5 vòng: pid thật (subprocess ngủ 60s) bị kill, temp của runtime và của profile bị xóa sạch, cả ba dict rỗng sau mỗi vòng; hàng đợi tay sống qua "restart" (manager mới trên cùng `base_dir` vẫn thấy mục PENDING), claim 2 lần bị chặn, fail→retry→complete chạy đúng, fail quá 3 lần → FAILED. Smoke thêm: `HumanBehaviour.plan_session`, `detect_block_page` và `launch_args` fail-closed khi không có proxy vẫn như cũ.

(đã nộp file `src/browser/profile_manager.py`)

(đã nộp file `tests/test_fix_browser_profile_geo_runtime.py`)

## File đã nộp

- `src/browser/profile_manager.py`
- `tests/test_fix_browser_profile_geo_runtime.py`
