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

fix_golden_path_a_script_alignment (duong A):
- data/golden/ la cay golden CHUAN duy nhat; fixture trong do la ban
  sao cua response API tho that va thuoc ve parser so huu no
  (connector Facebook, pipeline/connector TikTok, YouTube Data API).
- Cac builder o day sinh DUNG shape do: facebook theo VIDEO_FIELDS +
  insights top-level post_* cua endpoint video_insights rieng;
  tiktok theo response /video/list/ that (stats long, ten raw
  play_count/digg_count, cursor/has_more). Shape builder cu
  (total_* long trong data[0], field phang view_count/like_count)
  bi parser that parse thanh views=0/PARTIAL nen da bi loai bo.
- normalize() doc dung cac ten raw tren, nen build_golden_cases()
  PHAI bang data/golden/cases.json tren dia cho ca 3 platform;
  tests/test_fix_golden_path_a_script_alignment.py khoa dieu nay.
  Chay lai script khong con tai sinh drift giua hai cay golden.
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
    # Ban sao cua data/golden/facebook/video_list.json: response cua
    # VIDEO_FIELDS (id, title, description, created_time, permalink_url,
    # length, views, likes/comments .summary, shares) kem insights o
    # TOP LEVEL tu endpoint {video_id}/video_insights rieng voi cac
    # metric post_* — dung nhu src/connectors/facebook_connector.py
    # yeu cau va parse. post_video_view_time la TONG so giay xem.
    return {"data": [{"id": "987", "title": "T", "description": "D",
                      "created_time": "2026-11-20T10:00:00+00:00",
                      "permalink_url": "https://fb.watch/987",
                      "length": 60, "views": 100,
                      "likes": {"summary": {"total_count": 5}},
                      "comments": {"summary": {"total_count": 2}},
                      "shares": {"count": 1}}],
            "insights": [
                {"name": "post_video_views", "values": [{"value": 100}]},
                {"name": "post_video_view_time", "values": [{"value": 1234}]},
                {"name": "post_impressions", "values": [{"value": 500}]}],
            "paging": {}}


def tiktok_raw() -> dict:
    # Ban sao cua data/golden/tiktok/video_list.json: response that cua
    # /video/list/ — counter long trong "stats" voi ten raw
    # play_count/digg_count/comment_count/share_count/collect_count,
    # kem desc, cover_image_url, share_url, cursor va has_more.
    return {"data": {"videos": [{"id": "7512345678901234567",
                                 "title": "Meo tam su dem khuya",
                                 "desc": "meo tam su dem khuya",
                                 "cover_image_url": "https://example.com/cover.jpg",
                                 "create_time": 1763700000, "duration": 32,
                                 "share_url": "https://www.tiktok.com/@test/video/7512345678901234567",
                                 "stats": {"play_count": 1000,
                                           "digg_count": 120,
                                           "comment_count": 15,
                                           "share_count": 8,
                                           "collect_count": 20}}],
                     "cursor": "", "has_more": False}}


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
            st = v.get("stats", {})
            out.append({"platform": "tiktok", "platform_video_id": v["id"],
                        "title": v.get("title") or v.get("desc", ""),
                        "views": st.get("play_count", 0),
                        "likes": st.get("digg_count", 0),
                        "comments": st.get("comment_count", 0),
                        "shares": st.get("share_count", 0),
                        "saves": st.get("collect_count", 0),
                        "watch_time_seconds": 0, "source": "tiktok_api", "quality": "ok"})
    elif platform == "facebook":
        # Insights nam o TOP LEVEL (response cua video_insights gop vao),
        # ten post_*; neu fixture long trong tung video dang list thi doc
        # o do — giong cach connector chiu dung ca hai cach doc.
        top_ins = {d["name"]: d["values"][0]["value"]
                   for d in raw.get("insights", [])}
        for v in raw.get("data", []):
            nested = v.get("insights")
            ins = top_ins or (
                {d["name"]: d["values"][0]["value"] for d in nested}
                if isinstance(nested, list) else {})
            out.append({"platform": "facebook", "platform_video_id": v["id"],
                        "title": v.get("title") or v.get("description", ""),
                        "views": ins.get("post_video_views", v.get("views", 0)),
                        "likes": v.get("likes", {}).get("summary", {}).get("total_count", 0),
                        "comments": v.get("comments", {}).get("summary", {}).get("total_count", 0),
                        "shares": v.get("shares", {}).get("count", 0), "saves": 0,
                        "watch_time_seconds": ins.get("post_video_view_time", 0),
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

Golden chuan nam o data/golden/ (cay duy nhat); fixture o do la ban
sao response tho that thuoc ve parser so huu. Script nay sinh dung
shape do nen golden/ o day luon bang data/golden/ — chay lai script
khong tao drift.

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
