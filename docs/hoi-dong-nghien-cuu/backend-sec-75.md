# Sửa lỗi `fix/fix_youtube_quota_cache_channel`: YouTube quota dùng chung theo project, cache deepcopy, sửa channel_id

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp fix_youtube_quota_per_instance_and_chann + fix_youtube_cache_stale_and_mutable (backend-sec). Mỗi connector tự tạo QuotaTracker riêng trong khi hạn mức 10k units/ngày là theo Google Cloud project nên N worker tự nghĩ có N*10k; _channel_id đọc account.__dict__['payload'] không tồn tại trên PlatformAccount nên là dead code; _call trả cùng dict mutable từ cache và cache get_video_stats 300s làm snapshot mới mang số cũ. Fix: QuotaTracker thành bộ đếm dùng chung theo project_id (Redis/DB, atomic increment + roll theo ngày UTC của project), không truy cập được store quota thì fail-closed QuotaExceededError; trả deepcopy (hoặc bản JSON đã serialize) từ cache, cache=False mặc định cho videos.list statistics, chỉ cache metadata ít đổi, đưa account_id/project_id vào cache key và ghi TTL theo loại dữ liệu; thêm field channel_id rõ ràng (thêm trường có default vào PlatformAccount theo quy ước contracts hoặc bảng mapping account->channels riêng), xóa dead code payload. Test 2 connector cùng project chia sẻ một ngân sách quota.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_youtube_quota_cache_channel` (nhánh `fix/fix_youtube_quota_cache_channel`)**

Đã đọc bản mới nhất trên `main` trước khi sửa; chỉ nộp file mục tiêu + file test riêng, giữ nguyên toàn bộ hành vi hợp lệ cũ (batch 50 id, parse duration, analytics, `priority_score`, auth error mapping). Tóm tắt bản vá:

- **Quota dùng chung theo project:** `QuotaTracker` không còn là bộ đếm riêng của từng connector — nó thành facade trên một `QuotaStore` dùng chung theo `project_id` (mặc định lấy từ env `GOOGLE_CLOUD_PROJECT`/`YOUTUBE_PROJECT_ID`). Store cộng quota **nguyên tử** (check-and-add trong cùng một thao tác khóa/Lua, không có race đọc-rồi-ghi), khóa theo **ngày UTC** nên tự roll sang ngày mới. Hai backend: `InMemoryQuotaStore` (singleton cấp process làm mặc định — mọi tracker cùng project trong process chia nhau một ngân sách) và `RedisQuotaStore` (Lua `INCRBY` + kiểm tra giới hạn nguyên tử, key hết hạn sau 48h — bản production nhiều worker). Store lỗi/mất kết nối → `charge()` raise `QuotaExceededError`, **fail-closed**, không gọi API mù. `used`/`remaining`/`should_warn`/`day` đọc từ store chung nên connector nào cũng thấy cùng một con số.
- **Cache hết stale + hết mutable:** response được **deepcopy khi ghi vào cache và deepcopy khi trả ra** — caller sửa dict nhận được không làm hỏng cache, hai caller không bao giờ cầm cùng một dict. `videos.list` có part `statistics` **không bao giờ được cache** (kể cả khi caller truyền `cache=True` — policy chặn ở `_call`), nên snapshot mới luôn là số mới thay vì số cũ 300s; analytics/search cũng không cache. Chỉ metadata ít đổi mới được cache, với TTL theo loại: `channels.list` 3600s, `videos.list` không-statistics và `playlistItems.list` theo TTL của connector. Để việc này nhất quán, `list_videos` nay chỉ xin part metadata (`snippet,contentDetails,status`) — `_to_video` vốn không hề dùng statistics. **Cache key giờ chứa project_id + account_id + method + params**: trước đây `list_channels(mine=true)` của account B có thể nhận cache của account A vì key không có account.
- **channel_id hết dead code:** đoạn đọc `account.__dict__['payload']` (field không tồn tại trên `PlatformAccount`) đã bị xóa. Thứ tự phân giải rõ ràng: (1) bảng mapping account→channel riêng của connector (`register_channel()` / tham số `channel_map=`), (2) field `account.channel_id` nếu contracts khai báo (đọc bằng `getattr` nên tương thích cả khi contracts chưa có), (3) `account.platform_user_id`. Không sửa `contracts.py` vì quy tắc nhánh chỉ cho nộp file mục tiêu + test.

**Kiểm chứng trên VM Linux:** `pytest tests/test_fix_youtube_quota_cache_channel.py -q` → **13 passed**, gồm test đúng spec: 2 connector (mỗi cái một `QuotaTracker` riêng) cùng project + store dùng chung một ngân sách 150 — tổng chi chung là 102, lần `search.list` thứ hai từ connector còn lại bị `QuotaExceededError`, không còn chuyện N worker có N×10k; roll ngày UTC bằng `now_fn` tiêm vào; store hỏng → fail-closed; Redis store kiểm bằng fake client; statistics gọi 2 lần → transport chạy 2 lần và số views tăng thật; sửa dict trả về từ cache không độc cache; hai account không rò cache cho nhau; mapping thắng field thắng `platform_user_id`. Smoke thêm `get_video_stats_full`: views 500, watch 600s, followers_delta 2, ctr 0.025, quota 6 units — khớp số liệu các vòng trước.

(đã nộp file `src/connectors/youtube_connector.py`)

(đã nộp file `tests/test_fix_youtube_quota_cache_channel.py`)

## File đã nộp

- `src/connectors/youtube_connector.py`
- `tests/test_fix_youtube_quota_cache_channel.py`
