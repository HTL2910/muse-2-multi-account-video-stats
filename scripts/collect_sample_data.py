#!/usr/bin/env python3
"""approach_19 - Thu thap/xay dung du lieu mau, fixtures, golden, benchmark.

Chay duoc hoan toan offline bang stdlib:
    python scripts/collect_sample_data.py --out data/sample

- Sinh fixtures mau (anonymized, KHONG credential that) cho Facebook
  Graph API, TikTok API, YouTube Data API v3: raw response + ban chuan
  hoa ve UnifiedVideoMetric-like dict.
- Sinh golden cases co dinh cho CI (data/golden/*.json) de phat hien
  API doi schema: moi case co expected_video_count va expected metric.
- Benchmark cuc bo: do latency/success-rate cua ham parse chuan hoa
  theo platform (va mo phong theo proxy bang delay tiem vao), xuat CSV.
- Kèm README mo ta nguon, giay phep, cach tai tao.
Neu co bien moi truong SAMPLE_LIVE=1 va token cong khai tu chon, nguoi
van hanh co the tu thay transport fetch that; mac dinh KHONG goi mang,
KHONG luu bat ky secret nao.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from pathlib import Path

PLATFORMS = ("facebook", "tiktok", "youtube")

# ------------------------------------------------------------- fixtures
def facebook_raw() -> dict:
    return {"data": [{"id": "fb_video_001", "title": "Sample FB Video",
                      "created_time": "2026-01-01T00:00:00+0000",
                      "insights": {"data": [
                          {"name": "total_video_views", "values": [{"value": 12000}]},
                          {"name": "total_video_avg_time_watched",
                           "values": [{"value": 45000}]}]},
                      "likes": {"summary": {"total_count": 800}},
                      "comments": {"summary": {"total_count": 120}},
                      "shares": {"count": 45}}], "paging": {}}


def tiktok_raw() -> dict:
    return {"data": {"videos": [{"id": "tt_video_001", "title": "Sample TikTok",
                                 "create_time": 1767225600, "duration": 35,
                                 "view_count": 50000, "like_count": 4200,
                                 "comment_count": 310, "share_count": 190,
                                 "collect_count": 260}], "has_more": False}}


def youtube_raw() -> dict:
    return {"kind": "youtube#videoListResponse", "items": [{
        "id": "yt_video_001",
        "snippet": {"title": "Sample YT Video", "publishedAt": "2026-01-01T00:00:00Z",
                    "thumbnails": {"high": {"url": "https://example.invalid/thumb.jpg"}}},
        "contentDetails": {"duration": "PT3M20S"},
        "statistics": {"viewCount": "98000", "likeCount": "6100",
                       "commentCount": "540"}}]}


RAW_BUILDERS = {"facebook": facebook_raw, "tiktok": tiktok_raw, "youtube": youtube_raw}


def normalize(platform: str, raw: dict) -> list[dict]:
    """Chuan hoa toi thieu ve dang UnifiedVideoMetric (dict de test)."""
    out: list[dict] = []
    if platform == "youtube":
        for it in raw.get("items", []):
            st = it.get("statistics", {})
            out.append({"platform": "youtube", "platform_video_id": it["id"],
                        "title": it["snippet"]["title"],
                        "views": int(st.get("viewCount", 0)),
                        "likes": int(st.get("likeCount", 0)),
                        "comments": int(st.get("commentCount", 0)),
                        "shares": 0, "saves": 0, "watch_time_seconds": 0,
                        "source": "youtube_data_api", "quality": "ok"})
    elif platform == "tiktok":
        for v in raw.get("data", {}).get("videos", []):
            out.append({"platform": "tiktok", "platform_video_id": v["id"],
                        "title": v.get("title", ""), "views": v["view_count"],
                        "likes": v["like_count"], "comments": v["comment_count"],
                        "shares": v["share_count"], "saves": v.get("collect_count", 0),
                        "watch_time_seconds": 0, "source": "tiktok_api", "quality": "ok"})
    elif platform == "facebook":
        for v in raw.get("data", []):
            ins = {d["name"]: d["values"][0]["value"]
                   for d in v.get("insights", {}).get("data", [])}
            out.append({"platform": "facebook", "platform_video_id": v["id"],
                        "title": v.get("title", ""), "views": ins.get("total_video_views", 0),
                        "likes": v.get("likes", {}).get("summary", {}).get("total_count", 0),
                        "comments": v.get("comments", {}).get("summary", {}).get("total_count", 0),
                        "shares": v.get("shares", {}).get("count", 0), "saves": 0,
                        "watch_time_seconds": ins.get("total_video_avg_time_watched", 0) // 1000,
                        "source": "facebook_graph_api", "quality": "ok"})
    return out


def build_golden_cases() -> list[dict]:
    cases = []
    for p in PLATFORMS:
        metrics = normalize(p, RAW_BUILDERS[p]())
        cases.append({"id": f"golden-{p}-001", "platform": p,
                      "name": f"{p} video list basic",
                      "raw_fixture_path": f"data/golden/{p}/video_list.json",
                      "expected_video_count": len(metrics),
                      "expected_first_metric": metrics[0] if metrics else None})
    return cases


def benchmark(*, iterations: int = 200) -> list[dict]:
    rows = []
    for p in PLATFORMS:
        lat, ok = [], 0
        for _ in range(iterations):
            t0 = time.perf_counter()
            try:
                normalize(p, RAW_BUILDERS[p]())
                ok += 1
            except Exception:
                pass
            lat.append((time.perf_counter() - t0) * 1000)
        rows.append({"platform": p, "iterations": iterations,
                     "success_rate": ok / iterations if iterations else 0,
                     "latency_ms_mean": round(statistics.mean(lat), 4) if lat else 0,
                     "latency_ms_p95": round(sorted(lat)[int(len(lat) * 0.95) - 1], 4) if lat else 0,
                     "proxy": "local-parser (no network)"})
    return rows


README = """# Sample / Golden Data

Nguon: fixtures tu tao (synthetic, anonymized) mo phong dung schema
cong khai cua Facebook Graph API, TikTok API, YouTube Data API v3.
Khong chua credential, cookie, token hay du lieu nguoi dung that.
Giay phep: dung noi bo cho phat trien/CI cua du an.

Tai tao:
    python scripts/collect_sample_data.py --out data/sample

Cau truc:
- fixtures/<platform>/video_list.raw.json  : response tho mo phong
- fixtures/<platform>/normalized.json      : sau chuan hoa
- golden/cases.json                        : golden cases cho CI
- benchmark/benchmark.csv                  : latency/success-rate parser

Du lieu tho va du lieu chuan hoa luon tach rieng; golden case fail
nghĩa la schema/ parser da lech - phai xem lai connector truoc khi merge.
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="data/sample")
    ap.add_argument("--iterations", type=int, default=200)
    args = ap.parse_args()
    out = Path(args.out)
    for p in PLATFORMS:
        d = out / "fixtures" / p
        d.mkdir(parents=True, exist_ok=True)
        raw = RAW_BUILDERS[p]()
        (d / "video_list.raw.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        (d / "normalized.json").write_text(
            json.dumps(normalize(p, raw), ensure_ascii=False, indent=2),
            encoding="utf-8")
        g = out / "golden" / p
        g.mkdir(parents=True, exist_ok=True)
        (g / "video_list.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "golden").mkdir(parents=True, exist_ok=True)
    (out / "golden" / "cases.json").write_text(
        json.dumps(build_golden_cases(), ensure_ascii=False, indent=2),
        encoding="utf-8")
    rows = benchmark(iterations=args.iterations)
    bdir = out / "benchmark"
    bdir.mkdir(parents=True, exist_ok=True)
    with (bdir / "benchmark.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    (out / "README.md").write_text(README, encoding="utf-8")
    print(f"wrote sample data to {out}: platforms={PLATFORMS} rows={len(rows)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
