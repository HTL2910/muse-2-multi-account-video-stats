# Sửa lỗi `fix/fix_token_lock_distributed`: Token refresh dùng khóa phân tán thật có lease và timeout

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp fix_token_lock_not_distributed (backend-sec). _RefreshState chỉ dùng threading.Lock trong một process nên nhiều worker refresh cùng account đồng thời, refresh_token bị rotate có thể làm bên thua mất session; nhánh chờ lock không có timeout. Fix: thay bằng khóa phân tán thật (Redis SET NX PX hoặc Postgres advisory lock) có lease TTL, owner token và unlock an toàn; acquire phải có timeout và hết timeout thì fail-closed; sau khi lấy được lock phải đọc lại credential_ref/version từ store dùng chung và chỉ refresh nếu version chưa đổi. Test 2 TokenManager/process dùng chung store giả lập refresh cùng account.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_token_lock_distributed` (nhánh `fix/fix_token_lock_distributed`)**

Đã đọc bản mới nhất trên `main` (đã gồm fix KEK/fallback/wipe của vòng trước, giữ nguyên toàn bộ phần đó) và chỉ nộp file mục tiêu + file test riêng. Tóm tắt bản vá:

- **Khóa phân tán thật qua backend dùng chung** — giao diện mới `DistributedLock` với đủ thuộc tính khóa phân tán: lease TTL, owner token ngẫu nhiên cho mỗi lần acquire, `release` chỉ xóa khi owner khớp (compare-and-delete, không unlock nhầm khóa của worker khác), và `acquire` có timeout trả `None`. Ba backend kèm theo: `RedisDistributedLock` (Redis `SET NX PX` + unlock bằng Lua script kiểm owner — bản production), `PostgresAdvisoryLock` (`pg_try_advisory_lock` lặp có timeout, giữ connection theo session, unlock + trả connection), và `InMemoryDistributedLock` (đúng semantics Redis nhưng trong RAM, dùng cho test/đơn process). Mặc định `TokenManager` dùng **một instance in-memory dùng chung cấp process** cho mọi manager không inject backend — và docstring ghi rõ giới hạn: nhiều process thì bắt buộc inject Redis/Postgres dùng chung, nếu không khóa chỉ có tác dụng trong từng process.
- **Đọc lại version sau khi có khóa:** `ensure_fresh` ghi lại `credential_ref`/version trước khi acquire; sau khi lấy được khóa thì đọc lại bản ghi mới nhất từ store dùng chung (thêm `InMemoryVaultStore.find_latest` + `EncryptedSecretVault.latest_record/latest_ref/record_for`). Nếu ref/version đã đổi — worker khác đã refresh xong — thì dùng ngay token của bên thắng, **không gọi refresher lần hai** (đây là điểm gây mất session khi hai bên cùng rotate refresh_token). Ngay trước khi `rotate` còn kiểm tra thêm một lần nữa: nếu lease hết hạn giữa chừng và ref đã bị đổi, hủy rotate của mình và dùng kết quả của bên thắng.
- **Timeout thì fail-closed:** hết `lock_timeout_seconds` (mặc định 10s, TTL lease mặc định 60s, cả hai đều cấu hình được qua `lock_provider=`/`lock_ttl_seconds=`/`lock_timeout_seconds=`) thì tuyệt đối không refresh/rotate: token đã hết hạn → `NEEDS_REAUTH`; token còn hạn → trả token cũ kèm `TOKEN_EXPIRING` để caller lùi lại sau. Nhánh chờ vô hạn `st.lock.acquire()` của bản cũ đã bị xóa hẳn; `_RefreshState` giờ chỉ còn đếm lỗi.
- **Sửa kèm một lỗi nền lộ ra khi dùng store chung:** `EncryptedSecretVault.store()` trước đây đánh version bằng biến đếm riêng của từng instance — hai vault instance dùng chung một store sẽ cùng sinh `v{n}` trùng nhau. Nay version = max(biến đếm local, version cao nhất đã có trong store) + 1. Ref đã bị rotate mất cũng được `_read_current` tự nhảy sang ref mới nhất trong store thay vì ném lỗi.

**Kiểm chứng trên VM Linux:** `pytest` → **10 test mới passed** và chạy lại 10 test của fix vault vòng trước — **vẫn passed, không hồi quy**. Test chính đúng spec: 2 `TokenManager` riêng biệt dùng chung một vault store + một lock backend, refresh đồng thời cùng account với refresher chậm 0.25s → refresher chỉ chạy **đúng 1 lần**, cả hai account hội tụ về cùng một ref đã rotate; manager thứ hai cầm ref cũ (đã bị rotate mất) tự dùng token của bên thắng mà không refresh lại; khóa bị giữ bởi bên thứ ba + timeout ngắn → refresher không chạy, token hết hạn → NEEDS_REAUTH, token còn hạn → TOKEN_EXPIRING; unlock sai owner bị từ chối ở cả 3 backend (Redis/Postgres kiểm bằng fake client/connection); lease hết hạn thì khóa được cấp lại cho bên khác.

(đã nộp file `src/accounts/vault.py`)

(đã nộp file `tests/test_fix_token_lock_distributed.py`)

## File đã nộp

- `src/accounts/vault.py`
- `tests/test_fix_token_lock_distributed.py`
