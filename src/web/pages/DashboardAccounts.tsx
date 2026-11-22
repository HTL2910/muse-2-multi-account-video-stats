/**
 * approach_12 — Dashboard UX: multi-account overview + video detail.
 *
 * Stack (approach_13 contract): Next.js + TypeScript, TanStack Query for
 * server state, SSE (/api/events) for realtime, cursor pagination.
 * 4 screens: Accounts | Overview | Videos | Proxies+Jobs.
 * Types mirror src/contracts.py (Platform, AccountStatus, UnifiedVideoMetric).
 * No secret is ever rendered: the API returns credential *metadata* only.
 */
import React, { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

export type Platform = "facebook" | "tiktok" | "youtube";
export type AccountStatus =
  | "active" | "created" | "verified" | "token_expiring" | "refreshing"
  | "needs_review" | "needs_reauth" | "checkpoint" | "captcha_required"
  | "token_expired" | "rate_limited" | "banned" | "disabled" | "suspended";

export interface AccountRow {
  id: string; platform: Platform; display_name: string; avatar_url: string;
  status: AccountStatus; health_score: number; tags: string[]; group_ids: string[];
  proxy: { ip: string; country: string; latency_ms: number; status: string } | null;
  last_sync_at: string | null;
}
export interface VideoRow {
  id: string; account_id: string; platform: Platform; title: string;
  thumbnail_url: string; url: string; views: number; likes: number;
  comments: number; shares: number; velocity_per_hour: number; published_at: string;
}
export interface JobRow {
  id: string; job_type: string; account_id: string; status: string;
  attempts: number; last_error: string;
}
interface Page<T> { items: T[]; next_cursor: string | null; total: number | null }

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
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...init });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json() as Promise<T>;
}
const fmt = (n: number) => new Intl.NumberFormat("en", { notation: "compact" }).format(n);

function useEventStream(onEvent: () => void) {
  const ref = useRef(onEvent); ref.current = onEvent;
  useEffect(() => {
    const es = new EventSource("/api/events"); // SSE: account_status_changed | sync_completed | alert
    es.onmessage = () => ref.current();
    return () => es.close();
  }, []);
}

function BulkBar({ selected, onClear }: { selected: Set<string>; onClear: () => void }) {
  const qc = useQueryClient();
  const [progress, setProgress] = useState<string>("");
  const bulk = useMutation({
    mutationFn: async (action: string) => {
      const ids = [...selected];
      if (!window.confirm(`${action}: áp dụng cho ${ids.length} account?`)) throw new Error("cancelled");
      const results: Record<string, string> = {};
      let done = 0;
      for (const id of ids) { // per-item result, one failure never aborts the batch
        try {
          await api(`/api/accounts/${id}/${action}`, { method: "POST" });
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
      <button onClick={() => {
        const tag = window.prompt("Tag:"); if (tag)
          selected.forEach((id) => api(`/api/accounts/${id}/tags`, {
            method: "POST", body: JSON.stringify({ tag }) }));
      }}>Gán tag</button>
      <button onClick={onClear}>Bỏ chọn</button>
    </div>
  );
}

function AccountsScreen() {
  const [platform, setPlatform] = useState(""); const [status, setStatus] = useState("");
  const [q, setQ] = useState(""); const [selected, setSelected] = useState<Set<string>>(new Set());
  const query = useQuery({
    queryKey: ["accounts", platform, status, q],
    queryFn: () => api<Page<AccountRow>>(
      `/api/accounts?limit=200&platform=${platform}&status=${status}&search=${encodeURIComponent(q)}`),
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
              <span>{a.platform} · {a.display_name || a.id}
                {FAIL_CLOSED.includes(a.status) && <em> (fail-closed: job tự động đã dừng)</em>}</span>
              {a.proxy
                ? <span>🌐 {a.proxy.ip} · {a.proxy.country} · {a.proxy.latency_ms}ms</span>
                : <span style={{ color: "#dc2626" }}>no proxy</span>}
              <span>health {(a.health_score * 100).toFixed(0)}%</span>
              <span>{a.tags.join(", ")}</span>
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
  const kpi = useQuery({ queryKey: ["kpi"], queryFn: () => api<{
    views: number; likes: number; comments: number;
    by_platform: Record<Platform, number[]>; top_videos: VideoRow[];
  }>("/api/analytics/overview") });
  useEventStream(() => kpi.refetch());
  if (!kpi.data) return <p>Đang tải…</p>;
  return (
    <section>
      <div style={{ display: "flex", gap: 16 }}>
        <div>Views<strong>{fmt(kpi.data.views)}</strong></div>
        <div>Likes<strong>{fmt(kpi.data.likes)}</strong></div>
        <div>Comments<strong>{fmt(kpi.data.comments)}</strong></div>
      </div>
      {(Object.keys(kpi.data.by_platform) as Platform[]).map((p) => (
        <div key={p}>{p}<Sparkline points={kpi.data!.by_platform[p]} /></div>))}
      <h3>Top video tăng trưởng nhanh</h3>
      <ol>{kpi.data.top_videos.map((v) => (
        <li key={v.id}>{v.title} — {fmt(v.views)} views · {fmt(v.velocity_per_hour)}/h</li>))}</ol>
    </section>
  );
}

function VideosScreen() {
  const [sort, setSort] = useState("views"); const [openId, setOpenId] = useState<string>("");
  const videos = useQuery({ queryKey: ["videos", sort], queryFn: () =>
    api<Page<VideoRow>>(`/api/videos?limit=200&sort=${sort}`) });
  const history = useQuery({ queryKey: ["history", openId], enabled: !!openId, queryFn: () =>
    api<{ views: number }[]>(`/api/videos/${openId}/metrics`) });
  const exportCsv = () => { // CSV client-side; Excel export goes through /api/export (audited)
    const rows = videos.data?.items ?? [];
    const csv = ["title,platform,views,likes,comments,shares,velocity_per_hour",
      ...rows.map((v) => [v.title, v.platform, v.views, v.likes, v.comments, v.shares,
        v.velocity_per_hour].join(","))].join("\n");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = "videos.csv"; a.click();
  };
  return (
    <section>
      <label>Sort <select value={sort} onChange={(e) => setSort(e.target.value)}>
        <option value="views">views</option><option value="velocity">velocity</option>
        <option value="likes">likes</option></select></label>
      <button onClick={exportCsv}>Export CSV</button>
      <button onClick={() => api("/api/export/videos.xlsx", { method: "POST" })}>Export Excel</button>
      {(videos.data?.items ?? []).map((v) => (
        <div key={v.id} style={{ display: "flex", gap: 10 }}>
          <img src={v.thumbnail_url} alt="" width={96} height={54} loading="lazy" />
          <button onClick={() => setOpenId(v.id)}>{v.title}</button>
          <span>{fmt(v.views)} views · {fmt(v.velocity_per_hour)}/h</span>
          <a href={v.url} target="_blank" rel="noreferrer">Xem gốc ↗</a>
          {openId === v.id && history.data &&
            <Sparkline points={history.data.map((m) => m.views)} />}
        </div>))}
    </section>
  );
}

function ProxiesJobsScreen() {
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () =>
    api<Page<JobRow>>("/api/jobs?status=failed,dead_letter,retrying&limit=200") });
  const qc = useQueryClient();
  const retry = useMutation({ mutationFn: (id: string) =>
    api(`/api/jobs/${id}/retry`, { method: "POST" }),
    onSettled: () => qc.invalidateQueries({ queryKey: ["jobs"] }) });
  const proxies = useQuery({ queryKey: ["proxies"], queryFn: () =>
    api<Page<{ id: string; address: string; country: string; score: number; status: string }>>
      ("/api/proxies?limit=200") });
  return (
    <section>
      <h3>Proxy Manager</h3>
      {(proxies.data?.items ?? []).map((p) => (
        <div key={p.id}>{p.address} · {p.country} · score {p.score.toFixed(2)} · {p.status}</div>))}
      <h3>Job Monitor — failed / dead-letter</h3>
      <button onClick={() => (jobs.data?.items ?? []).forEach((j) => retry.mutate(j.id))}>
        Retry tất cả</button>
      {(jobs.data?.items ?? []).map((j) => (
        <div key={j.id}>{j.job_type} · account {j.account_id} · attempts {j.attempts} · {j.last_error}
          <button onClick={() => retry.mutate(j.id)}>Retry</button></div>))}
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
