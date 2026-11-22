"""Regression guard for fix_golden_path_a_script_alignment (duong A).

Truoc fix nay scripts/collect_sample_data.py la mot "may tai tao
drift": RAW_BUILDERS sinh shape builder tu che (facebook insights
total_* long trong data[0]; tiktok field phang view_count/like_count)
ma parser that (connector Facebook, ingestion TikTok) parse thanh
views=0 / PARTIAL, trong khi build_golden_cases() lai tro
raw_fixture_path vao data/golden — cay chua fixture that do parser
so huu. Hai cay golden vi the chua hai su that khac nhau.

Duong A (da khoa): data/golden la nguon chuan duy nhat va script hoc
lai shape that. Guard nay khoa ba dieu:

1. Moi RAW_BUILDERS[p]() bang (theo parsed JSON) fixture chuan
   data/golden/<p>/video_list.json — cho ca 3 platform.
2. build_golden_cases() == data/golden/cases.json tren dia (assert da
   phuc hoi cho ca 3 platform), va normalize() cho ra dung metric
   chuan (khong views=0, khong PARTIAL).
3. Chay lai main() vao thu muc tam: cay golden sinh ra van bang cay
   chuan — tai tao khong con la tai sinh drift, va cay sample
   (fixtures/normalized) khop metric chuan.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_spec = importlib.util.spec_from_file_location(
    "collect_sample_data", REPO_ROOT / "scripts" / "collect_sample_data.py")
csd = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(csd)

GOLDEN_DIR = REPO_ROOT / "data" / "golden"
PLATFORMS = ("facebook", "tiktok", "youtube")

CANONICAL_METRICS = {
    "facebook": {"platform": "facebook", "platform_video_id": "987",
                 "title": "T", "views": 100, "likes": 5, "comments": 2,
                 "shares": 1, "saves": 0, "watch_time_seconds": 1234,
                 "source": "facebook_graph_api", "quality": "ok"},
    "tiktok": {"platform": "tiktok",
               "platform_video_id": "7512345678901234567",
               "title": "Meo tam su dem khuya", "views": 1000,
               "likes": 120, "comments": 15, "shares": 8, "saves": 20,
               "watch_time_seconds": 0, "source": "tiktok_api",
               "quality": "ok"},
    "youtube": {"platform": "youtube", "platform_video_id": "yt_video_001",
                "title": "Sample YT Video", "views": 98000, "likes": 6100,
                "comments": 540, "shares": 0, "saves": 0,
                "watch_time_seconds": 0, "source": "youtube_data_api",
                "quality": "ok"},
}


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_builders_mirror_canonical_fixtures_for_all_platforms():
    for p in PLATFORMS:
        canonical = _load_json(GOLDEN_DIR / p / "video_list.json")
        assert csd.RAW_BUILDERS[p]() == canonical, (
            f"{p}: builder output drifted from data/golden/{p}/video_list.json "
            "— the script must mirror the parser-owned raw shape")


def test_build_golden_cases_equals_canonical_cases_file():
    canonical_cases = _load_json(GOLDEN_DIR / "cases.json")
    assert csd.build_golden_cases() == canonical_cases, (
        "build_golden_cases() disagrees with data/golden/cases.json — "
        "the two golden trees hold different truths again")


def test_normalize_yields_canonical_metrics_not_partial_zeros():
    for p in PLATFORMS:
        metrics = csd.normalize(p, csd.RAW_BUILDERS[p]())
        assert metrics == [CANONICAL_METRICS[p]], (
            f"{p}: normalize() = {metrics}, expected the canonical metric")
        assert metrics[0]["quality"] == "ok" and metrics[0]["views"] > 0


def test_old_builder_shapes_are_gone():
    fb = csd.facebook_raw()
    # Insights are the connector's top-level post_* list, never the
    # Page-level total_* family nested inside data[0].
    assert isinstance(fb.get("insights"), list)
    assert {d["name"] for d in fb["insights"]} == {
        "post_video_views", "post_video_view_time", "post_impressions"}
    assert "insights" not in fb["data"][0]
    tt_video = csd.tiktok_raw()["data"]["videos"][0]
    # Counters live nested in stats under the real raw names; the flat
    # normalized-layer names must not appear on the raw video.
    assert tt_video["stats"] == {"play_count": 1000, "digg_count": 120,
                                 "comment_count": 15, "share_count": 8,
                                 "collect_count": 20}
    assert "view_count" not in tt_video and "like_count" not in tt_video
    assert csd.tiktok_raw()["data"]["cursor"] == ""
    assert csd.tiktok_raw()["data"]["has_more"] is False


def test_regeneration_reproduces_canonical_golden_tree(tmp_path):
    argv = sys.argv
    sys.argv = ["collect_sample_data.py", "--out", str(tmp_path),
                "--iterations", "5"]
    try:
        assert csd.main() == 0
    finally:
        sys.argv = argv
    assert _load_json(tmp_path / "golden" / "cases.json") == \
        _load_json(GOLDEN_DIR / "cases.json")
    for p in PLATFORMS:
        assert _load_json(tmp_path / "golden" / p / "video_list.json") == \
            _load_json(GOLDEN_DIR / p / "video_list.json")
        assert _load_json(tmp_path / "fixtures" / p / "normalized.json") == \
            [CANONICAL_METRICS[p]]


if __name__ == "__main__":
    import tempfile
    test_builders_mirror_canonical_fixtures_for_all_platforms()
    test_build_golden_cases_equals_canonical_cases_file()
    test_normalize_yields_canonical_metrics_not_partial_zeros()
    test_old_builder_shapes_are_gone()
    with tempfile.TemporaryDirectory() as d:
        test_regeneration_reproduces_canonical_golden_tree(Path(d))
    print("ALL TESTS PASSED")
