# Sửa lỗi `fix/fix_scheduler_dead_letter_test_clock`: Viết lại test dead-letter bằng fake clock theo hành vi scheduled_at đã khóa

_algo-ml · Chuyên gia Thuật toán & AI · lần nộp 1_

## Việc được giao

Gộp 4 báo cáo (muse fix_scheduler_dead_letter_test_clock, systems-arch fix_scheduler_dead_letter_test_clock, algo-ml fix_scheduler_dead_letter_test_stale_clo, fullstack-ux fix_scheduler_m2_retry_deadletter_vs_sch). Lỗi ở TEST, code scheduler đúng: finish_job(ok=False) đặt job RETRYING với scheduled_at = now + backoff; pop_due chỉ trả job đã đến hạn (đúng fix scheduled_at đã phê duyệt); attempts chỉ tăng trong acquire_job — nên vòng lặp test acquire được None, nhánh 'or got' che mất None đó và finish lại cùng một object với attempts mãi 1, can_retry mãi đúng, trạng thái cuối RETRYING thay vì DEAD_LETTER. Tuyệt đối không nới pop_due. Fix test: inject FakeClock vào TaskSchedulerImpl(clock=...), sau mỗi lần finish fail thì tua clock quá delay backoff tối đa (60s là đủ cho 2**attempts*5s) rồi mới acquire lại; bỏ fallback 'or got' và assert acquire trả về job (không None) mỗi vòng; đổi vòng lặp hardcode 3 lần thành vòng lặp có giới hạn tới khi status là DEAD_LETTER (tối đa MAX_JOB_RETRIES+2 lần acquire) — không hardcode 3 hay 4 lần vì hai bản tái hiện mâu thuẫn nhau về số lần (algo-ml đo DEAD_LETTER sau đúng 3 lần acquire+fail); DỪNG vòng lặp ngay khi đã DEAD_LETTER — finish lại một job đã chết sẽ dead_letter.add() lần nữa và vỡ assertion len(dead_letter)==1 (đã kiểm chứng). Tách j2 sang video_id/scheduled_at khác để không bị dedup trong test này, hoặc assert tường minh enqueue(j2) trả về j1 kèm deduplicated=True (sau khi fix_scheduler_enqueue_dedup_protocol merge). Làm rõ đặc tả (không sửa ngưỡng): sửa docstring module 'retry tối đa 3 lần' gây mơ hồ thành đúng thực tế đã đo (3 lần chạy tổng cộng trước DLQ theo can_retry = attempts < MAX_JOB_RETRIES); chỉ sửa can_retry nếu test biên đếm số lần thực thi cho kết quả khác. Báo cáo off-by-one của backend-sec đã bị BÁC và tác giả đã rút lại — không sửa finish_job/can_retry theo hướng đó.

## Bài làm

Chỉ sửa TEST, không đụng scheduler (`pop_due` giữ nguyên). Test đã viết lại dùng `FakeClock` inject vào `TaskSchedulerImpl`, tua 60s qua mỗi backoff, bỏ fallback `or got`, vòng lặp có giới hạn (`MAX_JOB_RETRIES+2`) dừng ngay khi `DEAD_LETTER`, `j2` tách riêng (video khác, hẹn +1h nên không bị dedup và không cạnh tranh acquire). Đồng thời làm rõ đặc tả đã đo trong docstring module (3 lần chạy tổng cộng trước DLQ, `can_retry = attempts < MAX_JOB_RETRIES`) và bỏ ghi chú "local stub contracts.py" đã lỗi thời.

Kiểm chứng trên VM: test đã viết lại được trích nguyên văn từ file nộp (AST) và chạy **PASSED**; file test riêng mới **5 passed** (scenario viết lại, đếm đúng 3 lần chạy không có lượt thứ 4, retry không acquire được trước backoff, đặc tả finish-lại-job-chết add trùng dead-letter, vòng lặp cũ tái hiện `RETRYING`); toàn bộ suite local **141 passed**; file m2 đã viết lại qua `py_compile` OK. Lưu ý: file m2 đầy đủ cần CI chạy (nó import thêm `core/models`, `facebook_connector`, `monitor`, `api/main`, `fastapi` — module của đồng nghiệp không có trên VM này), nên logic của test viết lại đã được kiểm chứng nguyên văn qua file test riêng.

(đã nộp file `tests/test_systems_arch_m2.py`)

(đã nộp file `tests/test_fix_scheduler_dead_letter_test_clock.py`)

## File đã nộp

- `tests/test_systems_arch_m2.py`
- `tests/test_fix_scheduler_dead_letter_test_clock.py`
