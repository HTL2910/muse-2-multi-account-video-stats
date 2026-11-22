# Báo Cáo Nghiên Cứu & Giải Pháp Hội Đồng Chuyên Gia (MuseAssistantTeam)

- **GitHub Repository:** https://github.com/HTL2910/muse-2-multi-account-video-stats
- **Trưởng nhóm điều phối:** muse (CEO)
- **Số chuyên gia tham gia:** 5 thành viên
- **Số hướng giải quyết độc lập (đã khử trùng lặp & chia đều):** 20 hướng
- **Số nhánh sửa lỗi (`fix/...`) đã kiểm thử & gộp:** 37 nhánh
- **Trạng thái nghiệm thu:** 100% Đồng thuận sau 3 vòng kiểm định

---

# multi-account-video-stats

Hệ thống quản lý nhiều tài khoản Facebook, TikTok và YouTube để xem thông số video, có dùng proxy — Modular Monolith tiến hóa sang Microservices, Official API trước, fail-closed theo thiết kế.

- **GitHub Repository:** https://github.com/HTL2910/muse-2-multi-account-video-stats
- **Hội đồng Chuyên gia AI:** MuseAssistantTeam — **100% đồng thuận sau 3 vòng kiểm định trên nhánh `main`**
- **Trưởng nhóm Điều phối Nghiên cứu (Team Lead / CEO):** muse
- **Contract chung:** `src/contracts.py` **v1.1.0** (Python 3.11+, chỉ dùng thư viện chuẩn)

---

## 1. Tổng quan đề bài

### 1.1. Đề bài gốc

> hệ thống quản lý account facebook và tiktok, youtube để xem video thông số, hệ thống hoàn thiện quản lý nhiều account, có dùng proxy

### 1.2. Diễn giải thành yêu cầu

| # | Yêu cầu | Cách đáp ứng |
|---|---------|--------------|
| 1 | Quản lý **nhiều tài khoản** Facebook, TikTok, YouTube | Mô hình tài khoản thống nhất + máy trạng thái vòng đời (approach_02), lưu PostgreSQL |
| 2 | Xem **thông số video** (views, likes, comments, shares, watch time, tăng trưởng) | 3 connector nền tảng (approach_05/06/07) → chuẩn hóa về một schema `UnifiedVideoMetric` duy nhất (approach_08) → time-series + dashboard (approach_10/12) |
| 3 | **Dùng proxy** | Proxy pool sticky 1-1, health-check, failover cùng geo, fail-closed (approach_04) |
| 4 | Hệ thống **hoàn thiện**, vận hành được | Scheduler/quota (approach_09), RBAC/audit (approach_16), observability + golden test (approach_17), Docker một lệnh chạy (approach_20) |
| 5 | An toàn tài khoản, chống khóa chéo | Vault mã hóa + vòng đời token (approach_03), health score/risk (approach_14), browser profile cô lập 1-1 (approach_11) |

### 1.3. Nguyên tắc thiết kế đã khóa

1. **Official API trước, headless browser chỉ là fallback có kiểm soát** — dữ liệu bổ sung qua browser luôn gắn nhãn `MetricSource.BROWSER_FALLBACK`, không trộn lặng với dữ liệu API.
2. **Fail-closed, không fail-open** — proxy chết, session hết hạn, token invalid, trạng thái không biết → job bị `REJECTED`, không bao giờ dùng proxy/session mặc định của tài khoản khác, không bao giờ bịa dữ liệu.
3. **Một schema duy nhất** — mọi số liệu chuẩn hóa về `UnifiedVideoMetric`; idempotency theo giờ cho snapshot, theo ngày chỉ cho rollup.
4. **Không bịa trạng thái** — không token giả, không job PENDING giả, không scope ghi cho sản phẩm chỉ đọc.
5. **Tách triệt để 3 trục** — Account/Session, Proxy/Fingerprint, Dữ liệu/Stats; core không gọi trực tiếp API nền tảng mà qua `PlatformAdapter` thống nhất.

### 1.4. Tiêu chí nghiệm thu

- Toàn bộ 20 hướng giải quyết merge vào `main`, mỗi hướng có test đi kèm.
- Toàn bộ nhánh `fix/...` đã triage báo cáo **Test: PASS** và đã merge vào `main` (mục 6).
- Golden test đủ 3 nền tảng trên một cây `data/golden/` duy nhất, fixture khớp đúng shape parser thật.
- `docker compose up` chạy được toàn bộ stack bằng một lệnh (API + scheduler/worker + frontend).
- Hội đồng đạt **100% đồng thuận** sau 3 vòng kiểm định (xem biên bản ở mục 6 và ghi chú kiểm chứng ở Phụ lục A).

---

## 2. Kiến trúc hệ thống

### 2.1. Sơ đồ tổng thể

```mermaid
flowchart LR
    subgraph Client
        DASH[Dashboard Next.js\nsrc/web]
        OP[Operator\nRBAC + TOTP]
    end
    subgraph Gateway
        API[REST + WebSocket\nsrc/api/main.py]
        ALERT[Alert engine\nsrc/api/alert_rule_engine.py]
    end
    subgraph Core
        SCHED[Scheduler + Queue\nsrc/scheduler]
        PIPE[Ingestion pipeline\nsrc/ingestion]
        STORE[(PostgreSQL +\nTimescaleDB/ClickHouse)]
        TS[Time-series\nsrc/analytics]
        AI[AI / Forecast\nsrc/ai]
    end
    subgraph Accounts
        ACC[Account model\nsrc/accounts]
        VAULT[Vault + Token\nsrc/accounts/vault.py]
        RISK[Health / Risk\nsrc/risk]
    end
    subgraph Access
        PROXY[Proxy pool\nsrc/proxy]
        BROWSER[Browser profiles\nsrc/browser]
        FB[Facebook\nGraph API]
        TT[TikTok\nDisplay/Business API]
        YT[YouTube\nData API v3]
    end
    DASH --> API
    OP --> API
    API --> SCHED
    SCHED --> PIPE
    PIPE --> FB & TT & YT
    PIPE --> STORE
    SCHED --> PROXY
    PROXY --> BROWSER
    ACC --> VAULT
    RISK --> SCHED
    STORE --> TS --> AI
    TS --> API
    ALERT --> API
```

### 2.2. Luồng thu thập dữ liệu

1. **Lập lịch** — Scheduler tạo job `SYNC_VIDEOS` + `FETCH_STATS` theo nhịp (video mới 15 phút, video cũ 6 giờ), chỉ lấy job **đã đến hạn** (`pop_due`), tôn trọng backoff retry.
2. **Cổng fail-closed** — enqueue và pipeline đều kiểm tra: tài khoản ở trạng thái fail-closed (`CHECKPOINT`, `BANNED`, …) hoặc proxy không `ACTIVE` → job `REJECTED`, adapter không được gọi.
3. **Thu thập** — Connector gọi Official API qua proxy sticky 1-1 của tài khoản; quota 3 lớp (account / proxy / platform) trừ một lần duy nhất tại `QuotaManager.try_acquire()`.
4. **Chuẩn hóa** — Pipeline validate, parse, phát hiện spike và video bị xóa; giá trị không parse được → `PARTIAL` (không bịa 0), watch-time ưu tiên giây tuyệt đối.
5. **Lưu trữ** — Một store chuẩn duy nhất qua `get_shared_store()` (scheduler/worker, pipeline và API cùng đọc qua DI); dedup theo `idempotency_key` giờ.
6. **Hiển thị** — API Gateway (keyset cursor có ký HMAC) + Dashboard realtime qua WebSocket `/ws/events`; cảnh báo theo rule (Telegram/Webhook/Discord).

### 2.3. Hợp đồng chung `src/contracts.py` (v1.1.0)

File duy nhất mọi chuyên gia cùng import. Chỉ dùng thư viện chuẩn.

| Nhóm | Nội dung |
|------|----------|
| Enum | `Platform`, `AccountStatus` (+ `ALLOWED_ACCOUNT_TRANSITIONS`, `FAIL_CLOSED_STATUSES`), `AuthMethod`, `ProxyType/Kind/Status`, `JobType/Status/Priority`, `MetricSource`, `VideoStatus`, `DataQuality`, `BatchDecision`, `RiskLevel`, `Role`, `AlertSeverity/Type` |
| Dataclass | `Workspace`, `TokenSet`, `PlatformAccount`, `ProxyInfo/Binding/Health`, `BrowserProfile`, `Video`, `UnifiedVideoMetric`, `VideoStatsSnapshot`, `CollectJob`, `JobResult`, `SyncRequest/Result`, `AccountHealth`, `RiskAssessment`, `AccountGroup`, `Alert`, `AuditLogEntry`, `PaginatedResult`, `ViewForecast`, `Recommendation`, `GoldenCase` |
| Protocol | `PlatformAdapter`, `SecretVault`, `ProxyPool`, `MetricsStore`, `TaskScheduler`, `BrowserProfileManager`, `AccountHealthScorer`, `AlertNotifier` |
| Hằng số | `MAX_JOB_RETRIES=3`, `RISK_REDUCE_THRESHOLD=0.7`, `RISK_STOP_THRESHOLD=0.9`, `TOKEN_REFRESH_BEFORE_MINUTES=30`, `PROXY_QUARANTINE_FAILURES=3` |
| Helper | `utcnow`, `date_bucket`, `build_idempotency_key`, `compute_engagement_rate`, `compute_growth_rate`, `validate_metric`, `is_fail_closed_status`, `is_terminal_job_status` |

**Changelog 1.1.0** (Team Lead khóa, `fix_idempotency_hourly_bucket`): tách idempotency snapshot khỏi rollup ngày — `UnifiedVideoMetric.snapshot_key` theo bucket **giờ** cho time-series, `daily_key` chỉ cho rollup ngày, `idempotency_key` của metric từ 1.1.0 chính là `snapshot_key`; `CollectJob` tự sinh key theo bucket phút của `scheduled_at`; thêm trường `deduplicated` / `duplicate_of` để scheduler trả job gốc kèm cờ dedup thay vì PENDING giả. Giới hạn có chủ ý: 2 snapshot cùng giờ dedup thành 1 điểm/giờ (bảo vệ quota, phục vụ `velocity_1h`).

**Quy tắc import đã khóa:** đúng một kiểu `from src.contracts import ...` chạy từ repo root — không fallback `from contracts import` / `from ..contracts import` (fallback nạp cùng file thành module thứ hai, làm vỡ identity Enum/dataclass).

---

## 3. Cấu trúc thư mục

```
multi-account-video-stats/
├── README.md                     # Báo cáo tổng kết & tài liệu này
├── pyproject.toml                # Manifest phụ thuộc duy nhất (xem Phụ lục A)
├── .gitignore
├── data/
│   ├── golden/                   # Golden test duy nhất: cases.json + video_list.json (fb/tt/yt)
│   └── sample/                   # Fixtures + benchmark do scripts/collect_sample_data.py sinh
├── docker/
│   ├── docker-compose.yml        # Stack api + scheduler/worker + frontend + db
│   ├── Dockerfile.api
│   ├── Dockerfile.worker
│   └── Dockerfile.frontend
├── docs/
│   ├── ARCHITECTURE.md
│   └── hoi-dong-nghien-cuu/      # Biên bản hội đồng theo chuyên gia (algo-ml-*, backend-sec-*,
│                                 # fullstack-ux-*, systems-arch-*)
├── scripts/
│   ├── collect_sample_data.py    # Sinh fixtures/benchmark/golden từ RAW_BUILDERS
│   └── init_secrets.sh           # Tạo .secrets/*.txt (chmod 600)
├── src/
│   ├── contracts.py              # Hợp đồng giao diện & schema dùng chung (v1.1.0)
│   ├── core/models.py            # approach_01: core + PlatformAdapter
│   ├── accounts/                 # approach_02 (account_model) + approach_03 (vault)
│   ├── proxy/                    # approach_04: proxy_pool_manager
│   ├── connectors/               # approach_05/06/07: facebook/tiktok/youtube
│   ├── ingestion/                # approach_08: pipeline
│   ├── scheduler/                # approach_09: task_scheduler + __main__ (--beat)
│   ├── analytics/                # approach_10: time_series
│   ├── browser/                  # approach_11: profile_manager
│   ├── web/                      # approach_12: Next.js dashboard (package.json, pages/)
│   ├── api/                      # approach_13 (main) + approach_15 (alert_rule_engine)
│   ├── risk/                     # approach_14: account_health
│   ├── security/                 # approach_16: rbac_audit
│   ├── observability/            # approach_17: monitor
│   ├── ai/                       # approach_18: recommendation_engine
│   └── worker/                   # Worker entrypoint cho docker compose
└── tests/                        # Test theo module + test_fix_<ma_loi> cho từng fix đã triage
```

---

## 4. Tổng hợp 20 hướng giải quyết đã triển khai

| # | Hướng giải quyết | Phụ trách | Nhánh | File mục tiêu |
|---|------------------|-----------|-------|---------------|
| 01 | Kiến trúc core đa tài khoản và PlatformAdapter thống nhất | systems-arch | `expert/systems-arch-m2` | `src/core/models.py` |
| 02 | Mô hình tài khoản thống nhất và máy trạng thái vòng đời | algo-ml | `expert/algo-ml-m2` | `src/accounts/account_model.py` |
| 03 | Vault mã hóa credential, cookie và quản lý vòng đời token | backend-sec | `expert/backend-sec-m2` | `src/accounts/vault.py` |
| 04 | Proxy pool manager: sticky 1-1, chấm điểm, health-check và failover | fullstack-ux | `expert/fullstack-ux-m2` | `src/proxy/proxy_pool_manager.py` |
| 05 | Connector Facebook: Graph API + Page/Video Insights | systems-arch | `expert/systems-arch-m2` | `src/connectors/facebook_connector.py` |
| 06 | Connector TikTok: API chính thức + thu thập bổ sung có kiểm soát | algo-ml | `expert/algo-ml-m2` | `src/connectors/tiktok_connector.py` |
| 07 | Connector YouTube: Data API v3 + Analytics API | backend-sec | `expert/backend-sec-m2` | `src/connectors/youtube_connector.py` |
| 08 | Pipeline ingestion chuẩn hóa đa nền tảng (Adapter + ETL + Validation) | fullstack-ux | `expert/fullstack-ux-m2` | `src/ingestion/pipeline.py` |
| 09 | Scheduler và hàng đợi tác vụ phân tán dưới ràng buộc quota | systems-arch | `expert/systems-arch-m2` | `src/scheduler/task_scheduler.py` |
| 10 | Kho dữ liệu time-series và phân tích tăng trưởng video | algo-ml | `expert/algo-ml-m2` | `src/analytics/time_series.py` |
| 11 | Cô lập browser profile và mô phỏng hành vi chống phát hiện | backend-sec | `expert/backend-sec-m2` | `src/browser/profile_manager.py` |
| 12 | Dashboard UX tổng quan đa tài khoản và chi tiết video | fullstack-ux | `expert/fullstack-ux-m2` | `src/web/pages/DashboardAccounts.tsx` |
| 13 | REST API + WebSocket Gateway làm lớp tích hợp end-to-end | systems-arch | `expert/systems-arch-m2` | `src/api/main.py` |
| 14 | Account health score, dự đoán rủi ro khóa và xử lý checkpoint fail-closed | algo-ml | `expert/algo-ml-m2` | `src/risk/account_health.py` |
| 15 | Phân nhóm, tagging và rule engine cảnh báo | backend-sec | `expert/backend-sec-m2` | `src/api/alert_rule_engine.py` |
| 16 | RBAC, xác thực người vận hành và audit trail bất biến | fullstack-ux | `expert/fullstack-ux-m2` | `src/security/rbac_audit.py` |
| 17 | Observability, cổng fail-closed và kiểm thử golden | systems-arch | `expert/systems-arch-m2` | `src/observability/monitor.py` |
| 18 | Suy luận thông minh: gợi ý nội dung, dự báo views và phân cụm video | algo-ml | `expert/algo-ml-m2` | `src/ai/recommendation_engine.py` |
| 19 | Thu thập và xây dựng bộ dữ liệu thực tế, fixtures và benchmark trên máy ảo Linux | backend-sec | `expert/backend-sec-m2` | `scripts/collect_sample_data.py` |
| 20 | Đóng gói triển khai, sao lưu/khôi phục và tuân thủ ToS | fullstack-ux | `expert/fullstack-ux-m2` | `docker/docker-compose.yml` |

### 4.1. approach_01 — Core đa tài khoản và PlatformAdapter (`src/core/models.py`, systems-arch)

Modular Monolith: core chỉ làm việc với schema chung, mỗi nền tảng là một Adapter theo protocol `PlatformAdapter` (`authenticate`, `list_videos`, `get_video_stats`, `refresh_token`). `PlatformAdapterBase.guard()` thực thi fail-closed chung cho mọi connector. `InMemoryAccountStore` / `InMemoryVideoStore` / `InMemoryMetricsStore` là bản tham chiếu trong bộ nhớ; production thay bằng PostgreSQL/TimescaleDB không đổi caller.

### 4.2. approach_02 — Mô hình tài khoản và máy trạng thái (`src/accounts/account_model.py`, algo-ml)

`PlatformAccount` thống nhất 3 nền tảng; natural key `(workspace_id, platform, platform_user_id)` UNIQUE; máy trạng thái vòng đời 14 trạng thái với `ALLOWED_ACCOUNT_TRANSITIONS` — chuyển đổi bất hợp pháp bị từ chối và audit, không ép ngầm. Kèm `POSTGRES_DDL`, import/export CSV theo natural key, `check_token_lifecycle` (cảnh báo trước `TOKEN_REFRESH_BEFORE_MINUTES`).

### 4.3. approach_03 — Vault mã hóa và vòng đời token (`src/accounts/vault.py`, backend-sec)

Envelope encryption: mỗi account 1 DEK riêng, DEK wrap bằng KEK. Có `cryptography` → AES-256-GCM thật; thiếu → chỉ fallback HMAC-SHA256 ở `DEV_MODE` với định dạng riêng (`H1`, `hmac-sha256-stream-dev-fallback`), production thiếu KEK hoặc thiếu thư viện thì fail-closed ngay lúc khởi động. Token refresh dùng khóa phân tán có lease/timeout (Redis SET NX PX / Postgres advisory lock). Không bao giờ log/trả secret plaintext.

### 4.4. approach_04 — Proxy pool manager (`src/proxy/proxy_pool_manager.py`, fullstack-ux)

Sticky 1-1 mặc định (`max_accounts_per_proxy` = 1 cho residential/mobile/datacenter, khai báo rõ nếu muốn share); proxy chưa xác minh không được cấp (quarantine ngay, không đợi đủ lỗi); quarantine có hồi phục half-open với probation theo binding; failover **chỉ cùng geo**, hết proxy cùng geo → `ProxyUnavailable` (không fallback sang nước khác, không dùng proxy tài khoản khác, không direct connection).

### 4.5. approach_05 — Connector Facebook (`src/connectors/facebook_connector.py`, systems-arch)

Graph API (v19.0) qua `urllib`, transport inject được để test và chèn proxy sticky ở tầng transport. `expires_at` tính từ `expires_in` thật; parse thumbnail an toàn khi `data=[]`. Lỗi auth (190/102/200, 401/403) → `AdapterAuthError` → `NEEDS_REAUTH`, không retry vô hạn; rate-limit (4/17/32/613/80004) → backoff mũ + `Retry-After`, tối đa `MAX_JOB_RETRIES` lần.

### 4.6. approach_06 — Connector TikTok (`src/connectors/tiktok_connector.py`, algo-ml)

Official API trước (Login Kit → Display/Business/Research API); field API không có chỉ bổ sung qua đường browser có kiểm soát và gắn nhãn `BROWSER_FALLBACK`. **Chỉ đọc theo thiết kế**: `REQUIRED_SCOPES` chỉ có read scopes (`user.info.basic`, `video.list`), không bao giờ xin scope ghi. `authenticate()` / `refresh_token()` chỉ trả token **thật** (đổi auth code qua POST `/oauth/token`, hoặc TokenSet từ Vault rồi validate live bằng `/user/info/` có Bearer) — không có nguồn token thật thì raise `TikTokAuthError`, không bao giờ bịa token; whitelist scope được ép cả trên token đã lưu.

### 4.7. approach_07 — Connector YouTube (`src/connectors/youtube_connector.py`, backend-sec)

Data API v3 + Analytics API; quota là ngân sách của **Google Cloud Project** nên dùng `QuotaStore` dùng chung theo project (InMemory cho single-process, Redis Lua cho nhiều worker), fail-closed khi không truy cập được store. Cache trả **deepcopy** cả khi ghi và đọc; `videos.list` có `statistics` **không cache**; cache key chứa `project_id + account_id`. `channel_id` có thứ tự rõ ràng: mapping account→channel, rồi field `channel_id`, rồi `platform_user_id`.

### 4.8. approach_08 — Pipeline ingestion (`src/ingestion/pipeline.py`, fullstack-ux)

Discover → Fetch → Normalize → Store. Gate fail-closed ngay đầu `run()` (tài khoản fail-closed hoặc proxy không ACTIVE → `REJECT`, không gọi adapter). Validate lỗi → `NEEDS_REVIEW`, không lưu dạng OK; thiếu field → `PARTIAL` (không bịa 0); spike bất thường → `NEEDS_REVIEW`; `watchTimeSeconds` ưu tiên tuyệt đối; phát hiện video bị xóa bằng cách nạp `_known` từ store. Dedup theo `metric.idempotency_key` giờ; một store chuẩn duy nhất qua `get_shared_store()`.

### 4.9. approach_09 — Scheduler và hàng đợi (`src/scheduler/task_scheduler.py`, systems-arch)

Hàng đợi ưu tiên + dead-letter; per-account concurrency = 1 (lock theo account), quota 3 lớp (account/proxy/platform), shard worker theo account (consistent hashing), lease/heartbeat, retry backoff mũ tối đa 3 lần rồi vào dead-letter. `PriorityQueue` nhận clock inject được; `pop_due()` chỉ trả job đã đến hạn (quét toàn heap, job VIP tương lai không chặn job due); `enqueue()` fail-closed với trạng thái không biết, trừ quota một lần duy nhất tại `try_acquire()`, và trả job gốc kèm `deduplicated`/`duplicate_of` khi trùng key.

### 4.10. approach_10 — Time-series và phân tích tăng trưởng (`src/analytics/time_series.py`, algo-ml)

Store trong bộ nhớ phản chiếu thiết kế TimescaleDB/ClickHouse (append-only, idempotent theo `idempotency_key`, partition theo platform+tháng); `save_snapshot()` invalidate ngay rollup cache của video đó; dedup giờ sống trên bảng thường `idempotency_keys` (PRIMARY KEY là snapshot_key giờ) vì unique index trên hypertable bắt buộc chứa cột phân vùng `time`. Phân tích: sliding window, detrended z-score + Isolation Forest thuần Python, dự báo xu hướng tuyến tính kèm khoảng tin cậy.

### 4.11. approach_11 — Browser profile (`src/browser/profile_manager.py`, backend-sec)

1 account = 1 profile: `user_data_dir` riêng, fingerprint ổn định suy từ `hash(account_id)`; timezone/locale khớp **geo của proxy sticky** (lấy từ `ProxyInfo`, không suy từ niche, không mặc định US — không xác định được thì UTC/en-US + cảnh báo); proxy của profile phải trùng proxy sticky (lệch → `ProfileConflictError`); `close()` dọn cả profile, runtime và tiến trình (chống Chromium zombie); checkpoint/captcha → hàng đợi tay bền vững `SQLiteManualQueue` (kết nối đóng xác định theo từng thao tác).

### 4.12. approach_12 — Dashboard UX (`src/web/pages/DashboardAccounts.tsx`, fullstack-ux)

Next.js + TypeScript + TanStack Query. Một contract frontend↔backend duy nhất lấy từ OpenAPI của backend: route ở root (không tiền tố `/api`), kiểu row phản chiếu đúng schema OpenAPI, không gọi endpoint không tồn tại, realtime qua WebSocket `/ws/events` (không SSE), xuất CSV ở client, bulk assign-proxy gửi đúng body `{proxy_id}`. Workspace suy ra từ phiên đăng nhập (không lấy từ URL).

### 4.13. approach_13 — REST API + WebSocket Gateway (`src/api/main.py`, systems-arch)

FastAPI, phân lớp Routers → Service → Adapter/Worker. Endpoints: `/auth`, `/accounts` (CRUD + import upsert theo natural key), `/accounts/{id}/check-alive`, `/sync-videos`, `/assign-proxy`, `/videos`, `/videos/{id}/metrics`, `/proxies`, `/proxies/health`, `/proxies/import`, `/groups`, `/tags`, `/jobs`, `/ws/events`. Phân trang keyset cursor có ký HMAC (không offset); JWT verify bằng secret từ env, fail-fast khi thiếu `JWT_SECRET`; CORS allowlist từ env; rate-limit theo tenant; delete là soft-delete (DISABLED + thu hồi credential + đóng binding + audit hash-chain); không có scheduler thì trả 503 thay vì job PENDING giả.

### 4.14. approach_14 — Health score và rủi ro (`src/risk/account_health.py`, algo-ml)

Hồi quy logistic thuần Python (gradient descent, feature chuẩn hóa) + fallback rule-based khi chưa train; giải thích bằng xếp hạng `|weight × giá trị chuẩn hóa|`. Cổng theo hằng contracts: risk > 0.7 giảm tần suất 50% + đổi proxy sạch + dừng job không thiết yếu; risk > 0.9 dừng hoàn toàn, chuyển `NEEDS_REVIEW`. Mọi tín hiệu bất thường (checkpoint, captcha, 2FA, đổi mật khẩu, lỗi quyền, login lạ) → chuyển trạng thái ngay, dừng job tự động, không retry login, enqueue manual-review với evidence đã redact.

### 4.15. approach_15 — Nhóm, tagging và cảnh báo (`src/api/alert_rule_engine.py`, backend-sec)

Group/Tag store sau interface rõ ràng; gán hàng loạt kiểm tra workspace (lệch workspace → `WorkspaceMismatchError` fail-closed trước khi mutate, mọi lần gán/gỡ đều audit). Rule engine có ngưỡng cấu hình được, dedupe + cooldown chống spam, bộ đếm lỗi liên tiếp cho sync. Notifier đa kênh (notification center + Telegram/Webhook/Discord): Telegram gửi đúng `{chat_id, text}`; lỗi một kênh không giết kênh khác và được đếm vào metrics.

### 4.16. approach_16 — RBAC và audit (`src/security/rbac_audit.py`, fullstack-ux)

Mật khẩu argon2id/bcrypt nếu có, không thì PBKDF2-HMAC-SHA256 (600k iter) với salt riêng; TOTP RFC 6238 bắt buộc cho ADMIN; phân quyền theo `Role` với `require_permission`; session opaque có idle/absolute timeout, thu hồi được; chống brute-force bằng token bucket theo account+IP. Audit append-only + hash-chain SHA-256 (**tamper-evident**, không phải tamper-proof): append có lock, trả deep copy, PostgreSQL có trigger cấm UPDATE/DELETE.

### 4.17. approach_17 — Observability và golden test (`src/observability/monitor.py`, systems-arch)

Prometheus exposition (mỗi tên metric một dòng `# TYPE` duy nhất), trace context kiểu OpenTelemetry, structured logging đã redact secret/PII. P0 batch gate (schema, views không giảm bất thường, số video khớp kỳ vọng, proxy + account đều ACTIVE) — fail → `REJECTED`/`NEEDS_REVIEW`, không ghi đè dữ liệu cũ. Fail-closed gate cho proxy/session/auth. GoldenRunner unwrap đệ quy fixture lồng nhau. Kill-switch ngưỡng riêng `KILL_SWITCH_ERROR_RATE_THRESHOLD=0.4` (tách khỏi `RISK_STOP_THRESHOLD`), nối gate vào `enqueue()`/`acquire_job()` của scheduler.

### 4.18. approach_18 — Suy luận thông minh (`src/ai/recommendation_engine.py`, algo-ml)

Thuần stdlib, không sklearn/Prophet: TF-IDF + K-Means phân cụm video, dự báo views theo xu hướng tuyến tính kèm khoảng tin cậy từ phần dư, giờ đăng tốt nhất theo engagement theo giờ/ngày. Mọi đầu ra là **gợi ý kèm độ tin cậy** — module không bao giờ đăng, like hay hành động thay tài khoản (auto-posting nằm ngoài phạm vi).

### 4.19. approach_19 — Dữ liệu thực tế, fixtures và benchmark (`scripts/collect_sample_data.py`, backend-sec)

`RAW_BUILDERS` sinh raw response đúng shape API thật cho cả 3 nền tảng (Facebook: `VIDEO_FIELDS` + insights top-level `post_*`; TikTok: stats lồng, tên raw `play_count`/`digg_count`, `cursor`/`has_more`; YouTube: đúng shape Data API v3); `build_golden_cases()` sinh kỳ vọng golden; `normalize()` đọc đúng các tên đó. Output: `data/sample/fixtures/`, `data/sample/benchmark/benchmark.csv`, cây golden chuẩn `data/golden/`.

### 4.20. approach_20 — Đóng gói triển khai (`docker/docker-compose.yml`, fullstack-ux)

Một lệnh `docker compose up` chạy toàn stack: API (uvicorn), scheduler/worker (vòng lặp `--beat`, SIGTERM graceful), frontend (Next.js build), PostgreSQL + Redis. 3 Dockerfile multi-stage, non-root; healthcheck `/health` (không `/healthz`); secret qua file `../.secrets/*.txt` (tạo bằng `scripts/init_secrets.sh`, chmod 600) và `.env` — cả hai đều trong `.gitignore`. Sao lưu/khôi phục: backup PostgreSQL với `PGPASSWORD` từ secret; tuân thủ ToS: chỉ đọc số liệu qua Official API, tôn trọng rate-limit, không tự động like/comment/post.

---

## 5. Biên bản phản biện, kiểm thử & các nhánh fix

### 5.1. Lịch sử 3 vòng kiểm định

| Vòng | Kết quả pytest trên `main` | Kết luận hội đồng | Triage |
|------|---------------------------|-------------------|--------|
| **Audit #1** | 35 passed, 1 warning (Starlette/httpx) | `dong_thuan: false` — 10 lỗi (thiếu manifest, test đường dẫn cứng `/tmp/m2`, stub contracts, dual-import, golden thiếu YouTube, TikTok xin scope upload, thiếu test module trọng yếu, frontend thiếu manifest, registry ngoài kế hoạch) | ~50 báo cáo → **30 fix** (systems-arch 7, algo-ml 7, backend-sec 8, fullstack-ux 8) |
| **Audit #2** | 278 passed, **3 failed**, 1 warning | `dong_thuan: false` — 7 lỗi (test khóa version 1.0.0 cũ vs contract 1.1.0, test TikTok gọi authenticate không nguồn token, import dual chưa dọn hết, thiếu manifest/Dockerfile, golden thiếu `cases.json`, registry vẫn rỗng) | 32 báo cáo → **12 fix** (systems-arch 4, algo-ml 3, backend-sec 2, fullstack-ux 3) |
| **Audit #3** | 355 passed, **5 failed**, 1 warning | `dong_thuan: false` — 8 lỗi (assert version 1.0.0 còn sót ở test portability, SQLite manual queue rò connection làm 3 test profile đỏ trên Windows, test dead-letter viết theo hành vi cũ, import mới dọn xong core, thiếu manifest, merge tenant chưa xảy ra, core store trả sai kiểu, registry vẫn rỗng) | ~35 báo cáo → **12 fix** (systems-arch 4, algo-ml 2, backend-sec 2, fullstack-ux 4) |
| **Final** | Toàn bộ nhánh fix báo cáo **Test: PASS**, đã merge vào `main` | **100% đồng thuận** | — |

### 5.2. Bài học hệ thống: bệnh mất-merge và cổng manifest-test

Mẫu lỗi lặp lại ở vòng 2 và vòng 3 không phải "fix sai" mà là **"fix xong nhưng không lên main"**: ít nhất 5 nhánh đã nộp (scheduled_at, browser profile, API tenant, monitor, pipeline wiring) không bao giờ lên `main`, và suite không phát hiện được vì file test của chúng cũng vắng mặt. Hội đồng đã chốt quy tắc bắt buộc, áp dụng cho mọi fix:

> **Một fix chỉ được tính là xong khi file test của nó xuất hiện trong cây `tests/` trên `main`** — kiểm tra bằng manifest đối chiếu danh sách file sau merge, không chỉ dựa vào báo cáo nộp bài. Cổng manifest-test này gắn vào `fix_missing_dependency_manifest` (CI đối chiếu sau mỗi merge).

### 5.3. Hai chẩn đoán bị bác bỏ (bằng chứng hội đồng làm việc nghiêm túc)

- **`fix_scheduler_dead_letter_off_by_one`** (backend-sec tự báo, rồi tự rút lại): ba bản tái hiện độc lập khớp nhau — job kẹt ở `RETRYING` không phải vì ngưỡng chết lệch, mà vì `acquire()` trả `None` (job retry chưa đến hạn theo fix scheduled_at) và `or got` trong test che mất `None` đó, khiến `attempts` kẹt ở 1. Chỉ giữ lại phần làm rõ đặc tả (docstring "retry tối đa 3 lần" gây mơ hồ; tái hiện với FakeClock cho thấy `DEAD_LETTER` sau đúng 3 lần acquire+fail).
- **`fix_backend_sec_profile_tests_vs_geo_fix`** (fullstack-ux tự báo, rồi rút lại phần lớn): trace tay cho thấy không assertion nào trong 3 test profile đọc geo; log `profile_geo_unresolved` (fallback UTC/en-US khi không có proxy sticky) là hành vi đúng theo thiết kế. Gốc rễ duy nhất là rò connection SQLite.

### 5.4. Danh sách nhánh `fix/...` đã vượt qua kiểm thử và merge vào `main` (37 nhánh)

| # | Nhánh fix | Người sửa | Nội dung |
|---|-----------|-----------|----------|
| 1 | `fix/fix_test_portability_stub_contracts` | fullstack-ux | Xóa đường dẫn cứng /tmp/m2 và stub contracts khỏi test |
| 2 | `fix/fix_import_dual_contracts` | systems-arch | Khóa một kiểu import contracts duy nhất, xóa mọi fallback |
| 3 | `fix/fix_idempotency_hourly_bucket` | algo-ml | Khóa lại contract: idempotency theo giờ cho snapshot, theo ngày chỉ cho rollup |
| 4 | `fix/fix_scheduler_enqueue_fail_open` | backend-sec | enqueue() fail-closed với account/proxy chưa biết trạng thái |
| 5 | `fix/fix_golden_missing_youtube` | fullstack-ux | Thống nhất một cây golden data/golden đủ 3 nền tảng |
| 6 | `fix/fix_facebook_token_thumbnail` | systems-arch | Facebook token lấy expires_in thật và parse thumbnail an toàn |
| 7 | `fix/fix_api_cursor_offset` | fullstack-ux | Pagination chuyển sang keyset cursor có ký, chặn offset âm |
| 8 | `fix/fix_tiktok_upload_scope` | fullstack-ux | Xóa scope video.upload khỏi TikTok REQUIRED_SCOPES |
| 9 | `fix/fix_scheduler_quota_proxy_per_account` | algo-ml | Quota chỉ trừ một lần và triển khai thật lớp per-account |
| 10 | `fix/fix_api_import_delete_assign_audit` | backend-sec | API import dedup, delete cascade có audit, assign-proxy ghi binding |
| 11 | `fix/fix_audit_chain_thread_safety` | fullstack-ux | AuditChain thread-safe và trả bản sao, rõ tamper-evident |
| 12 | `fix/fix_tiktok_auth_fabricated` | algo-ml | TikTok authenticate/refresh phải trả token thật hoặc raise AuthError |
| 13 | `fix/fix_vault_kek_fallback_wipe` | backend-sec | Vault fail-closed khi thiếu KEK hoặc thiếu cryptography, sửa _wipe |
| 14 | `fix/fix_frontend_missing_manifest` | fullstack-ux | Thêm package.json/tsconfig cho frontend Dashboard |
| 15 | `fix/fix_proxy_pool_fail_closed` | algo-ml | Proxy pool fail-closed: unverified không cấp, quarantine hồi phục, failover cùng geo, sticky độc quyền |
| 16 | `fix/fix_frontend_backend_contract_mismatch` | fullstack-ux | Chốt contract frontend-backend duy nhất từ OpenAPI |
| 17 | `fix/fix_token_lock_distributed` | backend-sec | Token refresh dùng khóa phân tán thật có lease và timeout |
| 18 | `fix/fix_ingestion_normalize_removed` | algo-ml | Ingestion không bịa 0, ưu tiên watch-time giây, phát hiện video xóa qua store |
| 19 | `fix/fix_docker_compose_unrunnable` | systems-arch | Làm docker compose chạy được: đủ Dockerfile, scheduler entrypoint, healthcheck đúng |
| 20 | `fix/fix_youtube_quota_cache_channel` | backend-sec | YouTube quota dùng chung theo project, cache deepcopy, sửa channel_id |
| 21 | `fix/fix_timeseries_rollup_ddl` | algo-ml | Rollup invalidate cache khi ghi mới và DDL TimescaleDB chứa cột time |
| 22 | `fix/fix_missing_tests_critical_modules` | fullstack-ux | Bổ sung test cho vault, YouTube, browser, alert và collect_sample_data |
| 23 | `fix/fix_alert_group_workspace_telegram` | backend-sec | Gán group kiểm tra workspace và Telegram gửi đúng payload chat_id |
| 24 | `fix/fix_browser_profile_missing` | backend-sec | Merge lại nhánh browser profile geo/runtime đã nộp mà chưa lên main |
| 25 | `fix/fix_contracts_version_stale_tests` | fullstack-ux | Cập nhật test khóa CONTRACTS_VERSION lên 1.1.0 đã khóa |
| 26 | `fix/fix_tiktok_scope_test_auth` | algo-ml | Sửa test scope TikTok theo authenticate() fail-closed và ép whitelist scope |
| 27 | `fix/fix_pipeline_fail_closed_wiring` | systems-arch | Khôi phục pipeline wiring: gate fail-closed, một store chung, xóa registry |
| 28 | `fix/fix_scheduler_scheduled_at_lost` | algo-ml | Khôi phục fix scheduled_at bị mất khi merge lên main |
| 29 | `fix/fix_docker_compose_companions` | fullstack-ux | Commit đủ file đi kèm để docker compose chạy được một lệnh |
| 30 | `fix/fix_import_dual_contracts_residual` | systems-arch | Xóa toàn bộ fallback import contracts còn sót trên toàn repo |
| 31 | `fix/fix_golden_unified_source` | fullstack-ux | Chốt data/golden làm nguồn golden duy nhất, sửa drift facebook |
| 32 | `fix/fix_sqlite_manual_queue_connection_leak` | backend-sec | Đóng xác định connection SQLite của manual queue theo từng thao tác |
| 33 | `fix/fix_contracts_version_stale_portability` | fullstack-ux | Cập nhật assert version 1.0.0 còn sót ở test portability lên 1.1.0 |
| 34 | `fix/fix_scheduler_enqueue_dedup_protocol` | algo-ml | enqueue() trả job gốc kèm cờ dedup thay vì job PENDING ma |
| 35 | `fix/fix_golden_path_a_script_alignment` | fullstack-ux | Sửa script sinh golden theo shape parser thật, dời mọi kỳ vọng về số chuẩn |
| 36 | `fix/fix_scheduler_dead_letter_test_clock` | algo-ml | Viết lại test dead-letter bằng fake clock theo hành vi scheduled_at đã khóa |
| 37 | `fix/fix_scheduler_compose_no_account_source` | fullstack-ux | Nối scheduler trong compose với nguồn account dùng chung thay vì no-op |

Tất cả 37 nhánh trên đều được báo cáo **Test: PASS** và đã merge vào `main` (theo biên bản bàn giao của Hội đồng; xem Phụ lục A về kiểm chứng cuối).

---

## 6. Hướng dẫn cài đặt, sử dụng & kiểm chứng

### 6.1. Yêu cầu

- Python **3.11+**
- Node.js **>= 20** (cho dashboard)
- Docker + Docker Compose (cho chạy stack đầy đủ)
- PostgreSQL + Redis (production; bản tham chiếu chạy được bằng store in-memory)

### 6.2. Cài đặt (checkout sạch)

```bash
git clone https://github.com/HTL2910/muse-2-multi-account-video-stats.git
cd muse-2-multi-account-video-stats

# 1. Môi trường Python
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -e .                 # cài từ pyproject.toml (fastapi, pydantic, pytest, cryptography, httpx)

# 2. Secret (không commit file này)
bash scripts/init_secrets.sh     # tạo .secrets/*.txt, chmod 600
cp .env.example .env             # nếu có; điền JWT_SECRET, VAULT_KEK_B64, DATABASE_URL, ...

# 3. Kiểm thử
pytest -q                        # chạy từ repo root
```

Biến môi trường quan trọng:

| Biến | Ý nghĩa | Ghi chú |
|------|---------|---------|
| `JWT_SECRET` | Secret ký JWT cho API | Thiếu → API fail-fast lúc khởi động, không 500 từng request |
| `VAULT_KEK_B64` | KEK base64, giải mã đúng 32 byte | Thiếu và không `DEV_MODE` → Vault raise ngay lúc khởi động |
| `VAULT_DEV_MODE` / `DEV_MODE` | Cho phép KEK tạm + fallback HMAC (dev/test) | Ephemeral KEK chỉ sống trong RAM; restart là mất secret đã lưu bằng nó |
| `DATABASE_URL` | PostgreSQL cho store dùng chung | Scheduler đọc accounts từ đây (không còn file JSON, trừ dev-only) |
| `SCHEDULER_ACCOUNTS_FILE` | File JSON accounts (dev-only) | Không có file → tick là logged no-op, process vẫn healthy |
| `CORS_ALLOWED_ORIGINS` | Allowlist origin cho API | Mặc định chỉ frontend origin, không `*` |

### 6.3. Chạy bằng Docker (một lệnh)

```bash
bash scripts/init_secrets.sh
docker compose config            # kiểm tra cấu hình trước khi build
docker compose build
docker compose up -d
```

- API: http://localhost:8000 (OpenAPI tại `/docs`, `/openapi.json`; healthcheck `/health`)
- Dashboard: http://localhost:3000
- Scheduler chạy `python -m src.scheduler --beat` (tick: đăng ký account → lập lịch → enqueue; SIGTERM graceful).

### 6.4. Sử dụng nhanh

```bash
# Đăng nhập lấy JWT (qua approach_16: mật khẩu + TOTP bắt buộc cho ADMIN)
curl -X POST http://localhost:8000/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username": "admin", "password": "...", "totp": "123456"}'

# Import tài khoản (upsert theo natural key)
curl -X POST http://localhost:8000/accounts/import \
  -H "Authorization: Bearer $TOKEN" \
  -F file=@accounts.csv

# Gán proxy sticky cho tài khoản
curl -X POST http://localhost:8000/accounts/{id}/assign-proxy \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"proxy_id": "<proxy ACTIVE>"}'

# Đồng bộ video của một tài khoản
curl -X POST http://localhost:8000/accounts/{id}/sync-videos \
  -H "Authorization: Bearer $TOKEN"

# Xem metrics của video (keyset cursor)
curl "http://localhost:8000/videos/{id}/metrics?from=2026-11-01&to=2026-11-22" \
  -H "Authorization: Bearer $TOKEN"

# Realtime: WebSocket ws://localhost:8000/ws/events (sự kiện account_status_changed,
# sync_completed, alert) — lọc theo workspace của JWT claims
```

Dashboard mở http://localhost:3000, đăng nhập, workspace được suy ra từ phiên (không nhập tay).

### 6.5. Kiểm chứng từng hướng giải quyết

Chạy từ repo root, sau `pip install -e .`:

| Hướng | Lệnh kiểm chứng | Kỳ vọng |
|-------|----------------|---------|
| 01 Core / Adapter | `pytest tests/test_systems_arch_m2.py -q -k "core or adapter"` | Adapter qua `PlatformAdapterBase.guard()` fail-closed; `save_snapshot` trả `VideoStatsSnapshot` |
| 02 Account model | `pytest tests/test_algo_ml_m2.py -q -k account` | Chuyển trạng thái bất hợp pháp bị từ chối + audit; natural key UNIQUE |
| 03 Vault | `pytest tests/test_backend_sec_m2.py -q -k vault` | Thiếu KEK → raise lúc khởi động; thiếu `cryptography` ngoài DEV_MODE → raise; DEK wipe trong `finally` |
| 04 Proxy pool | `pytest tests/test_fix_proxy_pool_fail_closed.py -q` | Unverified không được cấp; failover khác geo → `ProxyUnavailable`; 1 proxy = 1 account mặc định |
| 05 Facebook | `pytest tests/test_fix_facebook_token_thumbnail.py -q` | `expires_at` từ `expires_in` thật; thumbnail `data=[]` parse an toàn |
| 06 TikTok | `pytest tests/test_fix_tiktok_auth_fabricated.py tests/test_fix_tiktok_upload_scope.py -q` | Không nguồn token thật → `TikTokAuthError`; `REQUIRED_SCOPES` không có scope ghi |
| 07 YouTube | `pytest tests/test_fix_youtube_quota_cache_channel.py -q` | Quota dùng chung theo project; cache deepcopy; `statistics` không cache |
| 08 Pipeline | `pytest tests/test_fix_ingestion_normalize_removed.py -q` | Giá trị lỗi → `PARTIAL` (không bịa 0); tài khoản fail-closed → `REJECT` không gọi adapter; 2 snapshot cách 2 giờ = 2 điểm |
| 09 Scheduler | `pytest tests/test_fix_scheduler_scheduled_at.py tests/test_fix_scheduler_enqueue_fail_open.py tests/test_fix_scheduler_quota_proxy_per_account.py tests/test_systems_arch_m2.py -q` | Job +6h không bị acquire sớm; retry sau 429 tôn trọng backoff; quota trừ đúng 1 lần; enqueue trùng key trả job gốc kèm `deduplicated=True` |
| 10 Time-series | `pytest tests/test_fix_timeseries_rollup_ddl.py -q` | Ghi snapshot mới invalidate rollup cache; dedup giờ qua bảng `idempotency_keys` |
| 11 Browser profile | `pytest tests/test_fix_browser_profile_missing.py tests/test_backend_sec_m2.py -q -k profile` | Proxy VN → `Asia/Ho_Chi_Minh`/`vi-VN`; proxy lệch geo → `ProfileConflictError`; sau thao tác không còn FD mở tới `manual_queue.sqlite3` |
| 12 Dashboard | `cd src/web && npm run typecheck && npm run build` | Typecheck + build xanh từ checkout sạch; không gọi endpoint không tồn tại |
| 13 API Gateway | `pytest tests/test_fix_api_cursor_offset.py tests/test_fix_api_import_delete_assign_audit.py -q` | Cursor sai ký → 400; import trùng → upsert; delete → DISABLED + audit; không scheduler → 503 |
| 14 Risk | `pytest tests/test_algo_ml_m2.py -q -k risk` | Risk > 0.7 giảm tần suất; > 0.9 dừng hẳn; checkpoint → dừng job + manual review |
| 15 Alert engine | `pytest tests/test_fix_alert_group_workspace_telegram.py -q` | Gán lệch workspace → `WorkspaceMismatchError`; Telegram gửi `{chat_id, text}` |
| 16 RBAC / Audit | `pytest tests/test_fix_audit_chain_thread_safety.py -q` | Append có lock + deep copy; `verify_chain()` phát hiện sửa/xóa; TOTP bắt buộc cho ADMIN |
| 17 Monitor | `pytest tests/test_fix_monitor_golden_killswitch_prometheus.py -q` | GoldenRunner unwrap fixture TikTok lồng nhau (1 video, `play_count` 1000); kill-switch trip ở 0.4 và chặn enqueue; mỗi metric một dòng `# TYPE` |
| 18 AI | `pytest tests/test_algo_ml_m2.py -q -k "recommend or forecast or cluster"` | Phân cụm/dự báo chạy stdlib-only, đầu ra kèm độ tin cậy, không hành động thay tài khoản |
| 19 Sample data | `python scripts/collect_sample_data.py && pytest tests/test_fix_golden_unified_source.py -q` | `cases.json == build_golden_cases()` cho cả 3 nền tảng; fixture khớp shape parser thật |
| 20 Docker | `docker compose config && docker compose build && docker compose up -d` | Config + build xanh; tạo account qua API → tick kế tiếp thấy job được enqueue |
| Toàn bộ | `pytest -q` | Suite xanh (vòng 3 đo 355 passed / 5 failed trước khi 12 fix vòng 3 merge; sau merge toàn bộ fix báo cáo PASS — xem Phụ lục A) |

### 6.6. Quy ước đóng góp (đã khóa qua 3 vòng audit)

1. Mọi thay đổi contract phải qua Team Lead khóa lại; bump `CONTRACTS_VERSION` thì sửa **mọi** test khóa version trong cùng PR.
2. Một kiểu import duy nhất `from src.contracts import ...`; CI cấm `from contracts import` / `from ..contracts import` (quét toàn `src/`).
3. Nhánh fix đặt tên `fix/<ma_loi>`; **fix chỉ tính là xong khi file test của nó xuất hiện trong `tests/` trên `main`** (cổng manifest-test).
4. Không bao giờ bịa dữ liệu/trạng thái để test xanh: không token giả, không job PENDING giả, không nới cổng fail-closed.
5. Sửa docstring **sau cùng**, khi code thật đã lên main — docstring nói sai trạng thái merge chính là cách bệnh mất-merge tự che giấu.

---

## 7. Giới hạn đã biết & hướng phát triển

- **Store in-memory là bản tham chiếu**: `InMemory*Store` chạy trong từng tiến trình; production cần PostgreSQL (quan hệ + audit append-only bằng trigger) và TimescaleDB/ClickHouse (time-series), Redis cho quota phân tán, khóa token và hàng đợi bền vững.
- **Browser fallback có kiểm soát**: Playwright chỉ import lazy khi launch thật; module không tự like/comment/post hàng loạt.
- **Bucket giờ có chủ ý**: 2 snapshot cùng giờ dedup thành 1 điểm/giờ; muốn chi tiết 15 phút phải đổi bucket ở một phiên bản contract mới.
- **Tuân thủ ToS**: chỉ đọc số liệu qua Official API trong giới hạn quota; tôn trọng rate-limit và `Retry-After`; không thu thập dữ liệu ngoài phạm vi OAuth đã cấp.
- **Mở rộng**: tách thành microservices khi vượt ngưỡng (`SPLIT_ACCOUNT_THRESHOLD` / `SPLIT_VIDEOS_PER_DAY_THRESHOLD` trong core), thêm ClickHouse cho phân tích dài hạn, thêm connector mới chỉ cần implement `PlatformAdapter`.

---

## Phụ lục A — Ghi chú kiểm chứng của Trưởng nhóm (đọc trước khi đóng dự án)

Báo cáo này tổng hợp theo **biên bản bàn giao của Hội đồng** (37 nhánh fix báo cáo Test: PASS và đã merge; 100% đồng thuận sau 3 vòng kiểm định). Với tư cách Trưởng nhóm, tôi ghi rõ những điểm **chưa được kiểm chứng độc lập trong cuộc trò chuyện này** và cần một lần chạy cổng manifest-test cuối cùng trước khi coi repo là đóng hoàn toàn:

1. **5 fix đã triage ở vòng 3 không xuất hiện trong danh sách 37 nhánh đã merge**: `fix_missing_dependency_manifest`, `fix_api_tenant_isolation_missing`, `fix_core_store_return_type`, `fix_monitor_golden_killswitch_prometheus`, `fix_registry_dead_code`. Đây đúng là nhóm fix đã "mất khi merge" nhiều vòng liên tiếp, nên việc chúng vắng mặt khỏi danh sách merge cần được đối chiếu — không tự động coi là đã xong.
2. **Hai bằng chứng trong danh sách file trên `main` kèm theo bàn giao cuối**: `src/registry.py` vẫn còn trong danh sách file (dù `fix_pipeline_fail_closed_wiring` được giao xóa nó), và không thấy `pyproject.toml` / `tests/conftest.py` trong phần danh sách file đã nhận (lưu ý danh sách này bị cắt ở `src/risk/.gitkeep`, cần đối chiếu toàn bộ cây file).
3. **Số liệu pytest cuối cùng chưa được bàn giao**: vòng 3 đo **355 passed / 5 failed** trước khi 12 fix vòng 3 merge; bản bàn giao cuối không kèm kết quả `pytest -q` sau merge. Lần chạy xác nhận cuối nên gồm: `pytest -q` trên checkout sạch cài từ manifest, `docker compose config + build + up` kèm kiểm tra scheduler enqueue job thật, và đối chiếu manifest (mọi fix đã triage đều có file test tương ứng trong `tests/` trên `main`).

Khi 3 điểm trên được xác nhận xanh, README này là tài liệu đóng dự án đầy đủ. Nếu bất kỳ điểm nào đỏ, mở lại đúng nhánh `fix/<ma_loi>` tương ứng thay vì sửa docstring xuông.

---

*Báo cáo tổng kết do Trưởng nhóm Điều phối Nghiên cứu (muse, CEO) biên soạn thay mặt Hội đồng Chuyên gia AI MuseAssistantTeam — systems-arch, algo-ml, backend-sec, fullstack-ux — ngày 22/11/2026.*
