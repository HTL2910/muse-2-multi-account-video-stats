# Sửa lỗi `fix/fix_alert_group_workspace_telegram`: Gán group kiểm tra workspace và Telegram gửi đúng payload chat_id

_backend-sec · Chuyên gia Backend & Bảo mật · lần nộp 1_

## Việc được giao

Gộp fix_alert_group_workspace_and_telegram_p (backend-sec). assign_bulk/remove_bulk chỉ nhận group_id + account_ids, không đối chiếu workspace_id của group với workspace của account nên workspace A có thể gán account của workspace B; WebhookNotifier kind='telegram' gửi payload {'text': ...} trong khi Telegram Bot API sendMessage bắt buộc chat_id + text nên sẽ bị 400, và MultiChannelNotifier nuốt exception vào list errors không ai đọc. Fix: assign/remove nhận thêm workspace_id và từ chối account không cùng workspace (fail-closed + audit); Telegram notifier nhận chat_id riêng trong cấu hình và gửi {'chat_id': ..., 'text': ...}; lỗi gửi kênh phải được đếm vào metrics/alert của observability chứ không chỉ nằm trong list errors. Test cross-workspace bị từ chối và test payload Telegram bằng fake sender.

## Bài làm

**backend-sec — COUNCIL_FIX_WORK `fix_alert_group_workspace_telegram` — BÁO CÁO**

Đã sửa đúng phương án hội đồng, chỉ trong `src/api/alert_rule_engine.py` (+ file test riêng):

**1. Group — kiểm tra workspace fail-closed + audit:**
- `assign_bulk`/`remove_bulk` nhận thêm `workspace_id` (workspace của caller) và `accounts` (registry: list `PlatformAccount` hoặc dict id → account/workspace_id). Ba trường hợp bị từ chối bằng exception mới `WorkspaceMismatchError` (kế thừa `PermissionError`): caller khác workspace với group; bất kỳ account nào thuộc workspace khác; account không tra được trong registry đã cung cấp (không xác minh được = không cho qua).
- Kiểm tra chạy **trước** khi mutate nên thao tác nguyên tử — không còn gán/gỡ một phần khi lẫn account của tenant khác.
- Mọi lần assign/remove (thành công lẫn bị từ chối, kèm lý do) đều ghi vào `audit_log` append-only của store + log cảnh báo có cấu trúc.

**2. Telegram — đúng payload Bot API:**
- `WebhookNotifier` nhận thêm field cấu hình `chat_id`; payload telegram nay là `{"chat_id": ..., "text": ...}` đúng contract `sendMessage`. Dự phòng tương thích cấu hình cũ: đọc `?chat_id=` trên URL. Thiếu hẳn chat_id thì `notify()` raise `ValueError` rõ ràng thay vì gửi payload chắc chắn bị 400. Payload discord/webhook giữ nguyên.

**3. Lỗi kênh không còn bị nuốt:**
- `MultiChannelNotifier` đếm mọi lần gửi vào `metrics` (`sent`/`failed` tổng và `by_channel`), có `failure_count`, log cảnh báo có cấu trúc cho từng lỗi, và gọi `metrics_sink(channel, exc, alert)` nếu được tiêm vào để observability đếm — list `errors` cũ vẫn giữ để tương thích. Một kênh lỗi vẫn không chặn các kênh còn lại.

**Kiểm chứng trên VM:** `pytest tests/test_fix_alert_group_workspace_telegram.py -q` → **12 passed** (cross-workspace assign/remove bị từ chối + không mutate + có audit, caller sai workspace bị từ chối, account lạ bị từ chối, registry dạng dict, payload telegram bằng fake sender có đúng `chat_id`+`text`, fallback chat_id từ URL, thiếu chat_id raise, payload discord/webhook không đổi, lỗi kênh vào metrics + sink trong khi kênh tốt vẫn nhận alert, và kịch bản 400 gốc của telegram). Smoke test `AlertRuleEngine.on_account_status` với notifier mới vẫn chạy đúng.

(đã nộp file `src/api/alert_rule_engine.py`)

(đã nộp file `tests/test_fix_alert_group_workspace_telegram.py`)

## File đã nộp

- `src/api/alert_rule_engine.py`
- `tests/test_fix_alert_group_workspace_telegram.py`
