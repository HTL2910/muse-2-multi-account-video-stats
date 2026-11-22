/**
 * approach_12 — Dashboard UX: multi-account overview + video detail.
 *
 * SINGLE frontend<->backend contract (fix_frontend_backend_contract_mismatch):
 * the source of truth is the FastAPI backend's OpenAPI (approach_13,
 * src/api/main.py — /openapi.json, /docs). This file is aligned to it:
 * - Routes live at the ROOT (no /api or /api/v1 prefix): /accounts,
 *   /videos, /jobs, /proxies, /proxies/health, /ws/events.
 * - GET /accounts REQUIRES ?workspace_id=... (taken from the page URL,
 *   default "default"); empty platform/status filters are omitted
 *   (FastAPI enums reject empty strings).
 * - Row types below mirror the OpenAPI response schemas EXACTLY
 *   (AccountOut, the /videos item dict, the /jobs item dict, Page).
 *   Fields the backend does not return (avatar_url, tags, group_ids, a
 *   joined proxy object, per-video view counts in the list) are NOT read
 *   here — video numbers come from GET /videos/{id}/metrics.
 * - Endpoints that do not exist are not called: no /analytics/overview
 *   (Overview aggregates the list endpoints client-side), no
 *   /export/*.xlsx (CSV export is client-side), no POST /jobs/{id}/retry,
 *   no POST /accounts/{id}/tags.
 * - Realtime is the backend's WebSocket /ws/events (there is no SSE
 *   endpoint); EventSource is not used.
 * - Bulk assign-proxy sends the body the backend requires,
 *   { "proxy_id": <first ACTIVE proxy id> }.
 * When the backend contract changes, regenerate types from /openapi.json
 * (e.g. openapi-typescript) instead of editing these interfaces by hand.
 * No secret is ever rendered: the API returns credential *metadata* only.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export type Platform = "facebook" | "tiktok" | "youtube";
export type AccountStatus =
  | "active" | "created" | "verified" | "token_expiring" | "refreshing"
  | "needs_review" | "needs_reauth" | "checkpoint" | "captcha_required"
  | "token_expired" | "rate_limited" | "banned" | "disabled" | "suspended";

/** Mirrors backend AccountOut (src/api/main.py) — no more, no less. */
export interface AccountRow {
  id: string; workspace_id: string; platform: Platform; platform_user_id: string;
  display_name: string; status: AccountStatus; health_score: number;
  proxy_binding_id: string | null; last_sync_at: string | null;
}
/** Mirrors the item dict of GET /videos. Metrics live under /videos/{id}/metrics. */
export interface VideoRow {
  id: string; account_id: string; platform: Platform; platform_video_id: string;
  title: string; url: string; published_at: string | null; status: string;
}
/** Mirrors the item dict of GET /videos/{video_id}/metrics. */
export interface MetricPoint {
  video_id: string; date_bucket: string; views: number; likes: number;
  comments: number; shares: number; watch_time_seconds: number; collected_at: string;
}
/** Mirrors the job dicts stored by the backend (GET /jobs items). */
export interface JobRow {
  id: string; job_type: string; account_id: string; status: string; created_at: string;
}
/** Mirrors the item dict of GET /proxies. */
export interface ProxyRow {
  id: string; host: string; port: number; country: string; status: string; score: number;
}
/** Mirrors one entry of GET /proxies/health (a bare array, not a Page). */
export interface ProxyHealth {
  id: string; status: string; success_rate: number;
  avg_latency_ms: number; consecutive_failures: number;
}
interface Page<T> { items: T[]; next_cursor: string | null; total: number }

const STATUS_COLOR: Record<AccountStatus, string> = {
  active: "#16a34a", verified: "#16a34a", created: "#64748b",
  token_expiring: "#d97706", refreshing: "#2563eb", rate_limited: "#d97706",
  needs_review: "#d97706", needs_reauth: "#dc2626", checkpoint: "#dc2626",
  captcha_required: "#dc2626", token_expired: "#dc2626", banned: "#991b1b",
  disabled: "#475569", suspended: "#991b1b",
};
const FAIL_CLOSED: AccountStatus[] = ["checkpoint", "captcha_required",
  "token_expired", "needs_reauth", "banned", "disabled", "suspended"];

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  try { // the gateway requires a Bearer JWT (approach_13/16); attach when present
    const token = window.localStorage.getItem("access_token");
    if (token) headers["Authorization"] = `Bearer ${token}`;
  } catch { /* storage unavailable (SSR/tests) — request goes out unauthenticated */ }
  const res = await fetch(path, { headers, ...init });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}
const fmt = (n: number) => new Intl.NumberFormat("en", { notation: "compact" }).format(n);

/** workspace_id is mandatory for GET /accounts: ?workspace_id= in the page URL, else "default". */
function currentWorkspaceId(): string {
  if (typeof window === "undefined") return "default";
  return new URLSearchParams(window.location.search).get("workspace_id") ?? "default";
}

function useEventStream(onEvent: () => void) {
  const ref = useRef(onEvent); ref.current = onEvent;
  useEffect(() => {
    // Backend realtime is WebSocket /ws/events ONLY (no SSE endpoint exists).
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/events`);
    ws.onmessage = () => ref.current(); // account_status_changed | sync_completed | alert
    return () => ws.close();
  }, []);
}

function BulkBar({ selected, onClear }: { selected: Set<string>; onClear: () => void }) {
  const qc = useQueryClient();
  const [progress, setProgress] = useState<string>("");
  const bulk = useMutation({
    mutationFn: async (action: string) => {
      const ids = [...selected];
      if (!window.confirm(`${action}: áp dụng cho ${ids.length} account?`)) throw new Error("cancelled");
      // assign-proxy REQUIRES a body {proxy_id} (backend 404/409 otherwise):
      // pick the first ACTIVE proxy from GET /proxies, fail per-item if none.
      let proxyId: string | null = null;
      if (action === "assign-proxy") {
        const proxies = await api<Page<ProxyRow>>("/proxies?limit=200");
        proxyId = proxies.items.find((p) => p.status === "active")?.id ?? null;
      }
      const results: Record<string, string> = {};
      let done = 0;
      for (const id of ids) { // per-item result, one failure never aborts the batch
        try {
          if (action === "assign-proxy") {
            if (!proxyId) throw new Error("no ACTIVE proxy available");
            await api(`/accounts/${id}/assign-proxy`,
              { method: "POST", body: JSON.stringify({ proxy_id: proxyId }) });
          } else {
            await api(`/accounts/${id}/${action}`, { method: "POST" });
          }
          results[id] = "ok";
        } catch (e) { results[id] = String(e); }
        setProgress(`${action}: ${++done}/${ids.length}`);
      }
      return results;
    },
    onSettled: () => { qc.invalidateQueries({ queryKey: ["accounts"] }); },
  });
  if (selected.size === 0) return null;
  return (
    <div role="toolbar" aria-label="Bulk actions" style={{ display: "flex", gap: 8 }}>
      <span>{selected.size} selected {progress}</span>
      <button onClick={() => bulk.mutate("check-alive")}>Check alive</button>
      <button onClick={() => bulk.mutate("sync-videos")}>Sync</button>
      <button onClick={() => bulk.mutate("assign-proxy")}>Gán proxy</button>
      <button onClick={onClear}>Bỏ chọn</button>
    </div>
  );
}

function AccountsScreen() {
  const [platform, setPlatform] = useState(""); const [status, setStatus] = useState("");
  const [q, setQ] = useState(""); const [selected, setSelected] = useState<Set<string>>(new Set());
  const query = useQuery({
    queryKey: ["accounts", platform, status, q],
    queryFn: () => {
      const params = new URLSearchParams({ workspace_id: currentWorkspaceId(), limit: "200" });
      if (platform) params.set("platform", platform); // omit empty: FastAPI enums reject ""
      if (status) params.set("status", status);
      if (q) params.set("search", q);
      return api<Page<AccountRow>>(`/accounts?${params.toString()}`);
    },
  });
  useEventStream(() => query.refetch());
  const rows = query.data?.items ?? [];
  // Virtual scroll: render only a window of rows for 1k+ accounts.
  const [scrollTop, setScrollTop] = useState(0); const ROW = 56, VIEW = 560;
  const start = Math.max(0, Math.floor(scrollTop / ROW) - 2);
  const visible = rows.slice(start, start + Math.ceil(VIEW / ROW) + 4);
  const toggle = (id: string) => setSelected((s) => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n;
  });
  return (
    <section>
      <div style={{ display: "flex", gap: 8 }}>
        <input aria-label="Search" placeholder="Tìm account…" value={q}
          onChange={(e) => setQ(e.target.value)} />
        <select aria-label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value)}>
          <option value="">All platforms</option><option>facebook</option>
          <option>tiktok</option><option>youtube</option></select>
        <select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">All statuses</option>
          {Object.keys(STATUS_COLOR).map((s) => <option key={s}>{s}</option>)}</select>
      </div>
      <BulkBar selected={selected} onClear={() => setSelected(new Set())} />
      <div onScroll={(e) => setScrollTop((e.target as HTMLDivElement).scrollTop)}
        style={{ height: VIEW, overflowY: "auto" }}>
        <div style={{ height: rows.length * ROW, position: "relative" }}>
          {visible.map((a, i) => (
            <div key={a.id} style={{ position: "absolute", top: (start + i) * ROW, height: ROW,
              display: "flex", gap: 10, alignItems: "center" }}>
              <input type="checkbox" checked={selected.has(a.id)} onChange={() => toggle(a.id)} />
              <span title={a.status} style={{ width: 10, height: 10, borderRadius: 5,
                background: STATUS_COLOR[a.status] }} />
              <span>{a.platform} · {a.display_name || a.platform_user_id}
                {FAIL_CLOSED.includes(a.status) && <em> (fail-closed: job tự động đã dừng)</em>}</span>
              {a.proxy_binding_id
                ? <span>🌐 proxy bound</span>
                : <span style={{ color: "#dc2626" }}>no proxy</span>}
              <span>health {(a.health_score * 100).toFixed(0)}%</span>
              <span>{a.last_sync_at ? `sync ${a.last_sync_at.slice(0, 10)}` : "never synced"}</span>
            </div>))}
        </div>
      </div>
      {query.isError && <p role="alert">Không tải được danh sách account.</p>}
    </section>
  );
}

function Sparkline({ points }: { points: number[] }) {
  const max = Math.max(...points, 1);
  const d = points.map((p, i) => `${(i / Math.max(1, points.length - 1)) * 100},${30 - (p / max) * 28}`).join(" ");
  return <svg width={300} height={32} aria-hidden><polyline points={d} fill="none" stroke="currentColor" /></svg>;
}

function OverviewScreen() {
  // No /analytics/overview endpoint exists: aggregate the real list
  // endpoints client-side (totals come from each Page's `total`).
  const accounts = useQuery({ queryKey: ["overview-accounts"], queryFn: () =>
    api<Page<AccountRow>>(`/accounts?workspace_id=${encodeURIComponent(currentWorkspaceId())}&limit=1`) });
  const videos = useQuery({ queryKey: ["overview-videos"], queryFn: () =>
    api<Page<VideoRow>>("/videos?limit=200") });
  const health = useQuery({ queryKey: ["overview-proxy-health"], queryFn: () =>
    api<ProxyHealth[]>("/proxies/health") });
  const jobs = useQuery({ queryKey: ["overview-jobs"], queryFn: () =>
    api<Page<JobRow>>("/jobs?limit=200") });
  useEventStream(() => { accounts.refetch(); videos.refetch(); jobs.refetch(); });
  const failedJobs = (jobs.data?.items ?? []).filter((j) =>
    ["failed", "dead_letter", "retrying"].includes(j.status)).length;
  const healthyProxies = (health.data ?? []).filter((p) => p.status === "active").length;
  return (
    <section>
      <div style={{ display: "flex", gap: 16 }}>
        <div>Accounts<strong>{accounts.data?.total ?? "…"}</strong></div>
        <div>Videos<strong>{videos.data?.total ?? "…"}</strong></div>
        <div>Proxy active<strong>{healthyProxies}/{(health.data ?? []).length}</strong></div>
        <div>Job lỗi<strong>{failedJobs}</strong></div>
      </div>
      <h3>Video mới nhất</h3>
      <ol>{(videos.data?.items ?? []).slice(0, 10).map((v) => (
        <li key={v.id}>{v.title || v.platform_video_id} — {v.platform}
          {v.published_at ? ` · ${v.published_at.slice(0, 10)}` : ""}</li>))}</ol>
    </section>
  );
}

function VideosScreen() {
  const [sort, setSort] = useState("newest"); const [openId, setOpenId] = useState<string>("");
  const videos = useQuery({ queryKey: ["videos"], queryFn: () =>
    api<Page<VideoRow>>("/videos?limit=200") });
  const history = useQuery({ queryKey: ["history", openId], enabled: !!openId, queryFn: () =>
    api<Page<MetricPoint>>(`/videos/${openId}/metrics?limit=200`) });
  const rows = useMemo(() => { // /videos has no sort param: sort client-side
    const items = [...(videos.data?.items ?? [])];
    if (sort === "title") items.sort((x, y) => x.title.localeCompare(y.title));
    else items.sort((x, y) => (y.published_at ?? "").localeCompare(x.published_at ?? ""));
    return items;
  }, [videos.data, sort]);
  const exportCsv = () => { // CSV client-side (no /export endpoint exists on the backend)
    const csv = ["title,platform,platform_video_id,published_at,url",
      ...rows.map((v) => [v.title, v.platform, v.platform_video_id,
        v.published_at ?? "", v.url].join(","))].join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = "videos.csv"; a.click();
  };
  return (
    <section>
      <label>Sort <select value={sort} onChange={(e) => setSort(e.target.value)}>
        <option value="newest">newest</option><option value="title">title</option>
      </select></label>
      <button onClick={exportCsv}>Export CSV</button>
      {rows.map((v) => (
        <div key={v.id} style={{ display: "flex", gap: 10 }}>
          <button onClick={() => setOpenId(v.id)}>{v.title || v.platform_video_id}</button>
          <span>{v.platform} · {v.published_at ? v.published_at.slice(0, 10) : ""}</span>
          <a href={v.url} target="_blank" rel="noreferrer">Xem gốc ↗</a>
          {openId === v.id && history.data &&
            <Sparkline points={history.data.items.map((m) => m.views)} />}
        </div>))}
    </section>
  );
}

function ProxiesJobsScreen() {
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () =>
    api<Page<JobRow>>("/jobs?limit=200") }); // backend filters by account_id only
  const proxies = useQuery({ queryKey: ["proxies"], queryFn: () =>
    api<Page<ProxyRow>>("/proxies?limit=200") });
  const jobRows = jobs.data?.items ?? [];
  const failed = jobRows.filter((j) => ["failed", "dead_letter", "retrying"].includes(j.status));
  const shownJobs = failed.length > 0 ? failed : jobRows; // status filter is client-side
  return (
    <section>
      <h3>Proxy Manager</h3>
      {(proxies.data?.items ?? []).map((p) => (
        <div key={p.id}>{p.host}:{p.port} · {p.country} · score {p.score.toFixed(2)} · {p.status}</div>))}
      <h3>Job Monitor — failed / dead-letter</h3>
      {shownJobs.map((j) => (
        <div key={j.id}>{j.job_type} · account {j.account_id} · {j.status} · {j.created_at}</div>))}
      {jobRows.length === 0 && <p>Chưa có job nào.</p>}
    </section>
  );
}

export default function DashboardAccounts() {
  const [tab, setTab] = useState<"accounts" | "overview" | "videos" | "ops">("overview");
  const [dark, setDark] = useState(false);
  const theme = useMemo(() => ({ background: dark ? "#0f172a" : "#ffffff",
    color: dark ? "#e2e8f0" : "#0f172a", minHeight: "100vh", padding: 16 }), [dark]);
  return (
    <main style={theme}>
      <nav style={{ display: "flex", gap: 8 }}>
        <button onClick={() => setTab("overview")}>Dashboard</button>
        <button onClick={() => setTab("accounts")}>Accounts</button>
        <button onClick={() => setTab("videos")}>Video Explorer</button>
        <button onClick={() => setTab("ops")}>Proxy + Jobs + Settings</button>
        <button onClick={() => setDark((d) => !d)}>{dark ? "☀ Light" : "🌙 Dark"}</button>
      </nav>
      {tab === "overview" && <OverviewScreen />}
      {tab === "accounts" && <AccountsScreen />}
      {tab === "videos" && <VideosScreen />}
      {tab === "ops" && <ProxiesJobsScreen />}
    </main>
  );
}
