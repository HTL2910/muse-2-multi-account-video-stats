# Sửa lỗi `fix/fix_proxy_pool_fail_closed`: Proxy pool fail-closed: unverified không cấp, quarantine hồi phục, failover cùng geo, sticky độc quyền

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp fix_proxy_healthcheck_unverified_still_u (algo-ml) + fix_proxy_quarantine_no_recovery_and_geo + fix_proxy_not_exclusive_sticky (fullstack-ux). health_check() khi không có checker trả ok=False nhưng không đổi status nên proxy vẫn ACTIVE và vẫn được cấp; _usable() chỉ chấp nhận ACTIVE/WARMUP và không có đường quay lại từ QUARANTINED nên pool teo tới rỗng; _candidates() trả toàn bộ pool khi hết proxy cùng geo nên failover lén sang quốc gia khác và ghi sai reason='failover_same_geo', đồng thời đặt WARMUP trên ProxyInfo dùng chung làm hạ trạng thái các account khác; _pick/acquire không loại proxy đã có binding active của account khác nên nhiều account chung 1 IP. Fix: khi không có checker thì đánh dấu proxy QUARANTINED hoặc loại khỏi _candidates() cho tới khi được kiểm chứng, bắt buộc health_checker ở production; _candidates khi country khác rỗng mà không có proxy cùng geo phải trả [] để failover ném ProxyUnavailable; thêm reactivation: hết circuit_open_until thì probe, thành công -> WARMUP probation (concurrency 1) rồi mới ACTIVE, trạng thái warm-up nằm trên ProxyBinding/account chứ không nằm trên ProxyInfo dùng chung; mặc định cấp phát độc quyền (loại proxy đã có binding/lease active của account khác), nếu chủ đích share thì khai báo rõ max_accounts_per_proxy theo kind (mặc định 1 cho residential/mobile, Team Lead đã chốt), hiển thị số account đang share và hạ per_proxy_concurrency xuống 1 cho proxy đã share.

## Bài làm

Đã tái hiện cả 4 lỗi trên máy ảo Linux trước khi fix: (1) health_check không checker trả ok=False nhưng proxy vẫn ACTIVE và vẫn được cấp; (2) failover trả proxy US khi proxy VN chết và đặt WARMUP lên ProxyInfo dùng chung; (3) proxy QUARANTINED không bao giờ quay lại; (4) 2 account chung 1 proxy duy nhất.

Đã chạy kiểm thử trên máy ảo Linux (venv pytest 9.2.0, Python 3.12.3): **54 passed** — 12 test mới `test_fix_proxy_pool_fail_closed.py` + 17 test `test_algo_ml_m2.py` + 10 test idempotency + 15 test TikTok auth, không regression.

Thay đổi chính trong `src/proxy/proxy_pool_manager.py` (dựa trên `main` như đã đọc, giữ nguyên parse/scoring/lease):
- Unverified không được cấp: health_check không có checker, hoặc checker trả bad (not ok / ip_leak / dns_leak / blacklisted), thì QUARANTINE ngay lập tức (không đợi 3 lần lỗi) và loại khỏi `_candidates()` cho tới khi kiểm chứng thật. `require_health_checker=True` thì `__init__` raise nếu thiếu checker; `require_verified=True` thì chỉ cấp proxy đã health-check ok ít nhất 1 lần.
- Quarantine có hồi phục: hết `circuit_open_until` thì probe lại bằng health_checker (trong `_candidates()` và `reactivate_due_proxies()`); probe ok → ACTIVE, account nhận proxy đó vào probation WARMUP theo binding/account (`is_probation()`/`probation_proxy()`, hết sau 2 request ok), không đặt WARMUP lên ProxyInfo dùng chung nữa; probe fail → quarantine lại với backoff tăng; không có checker thì không bao giờ tự phục hồi.
- Failover cùng geo thật: `_candidates(country)` chỉ trả proxy cùng geo (hết thì trả `[]` → `ProxyUnavailable`), không lén sang quốc gia khác; failover không chạm `status` của ProxyInfo thay thế, chỉ đánh dấu probation trên binding của account đó với reason đúng.
- Độc quyền mặc định: `_pick`/`acquire` loại proxy đã có binding của account khác; muốn share phải khai báo rõ `max_accounts_per_proxy` theo kind (mặc định 1 cho residential/mobile/datacenter); `accounts_sharing()`/`sharing_count()` hiển thị số account đang share, `effective_proxy_concurrency()` hạ xuống 1 khi proxy đã share.

(đã nộp file `src/proxy/proxy_pool_manager.py`)

(đã nộp file `tests/test_fix_proxy_pool_fail_closed.py`)

## File đã nộp

- `src/proxy/proxy_pool_manager.py`
- `tests/test_fix_proxy_pool_fail_closed.py`
