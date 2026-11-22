"""fix_golden_unified_source — data/golden/ is THE canonical golden
tree (cases.json lives there). Two cross-scope merges defined what
"canonical" means per platform, and the answer is NOT "whatever
scripts/collect_sample_data.py generates" — for two of the three
platforms the script is the drifted side.

YOUTUBE (builder-canonical): the on-disk fixture equals
RAW_BUILDERS["youtube"]() and its case reproduces through the
script's normalize(). No other consumer claims a different shape.

FACEBOOK (connector-canonical, systems-arch): the fixture is a
CONNECTOR test double — VIDEO_FIELDS (id,title,description,
created_time,permalink_url,length,views,...) plus a top-level
insights list doubling the separate {video_id}/video_insights
endpoint, with INSIGHT_METRICS post_video_views / post_video_
view_time / post_impressions. The builder shape (total_* names,
avg_time_watched) parses on the real connector to views=0 /
PARTIAL. Kept verbatim: id "987", views 100, likes 5, comments 2,
shares 1, watch 1234s, impressions 500.

TIKTOK (real-API-canonical, algo-ml): the fixture mirrors a real
TikTok /video/list/ response — numeric-string id, counters nested
in `stats` under the RAW names (play_count, digg_count,
comment_count, share_count, collect_count) that both the connector's
map_metric and the ingestion RAW_FIELD_MAP read, plus desc /
cover_image_url / share_url and data-level cursor/has_more.
tests/test_algo_ml_m2.py pins it (views 1000, likes 120, saves 20,
engagement_rate 0.143). The builder's flat view_count/like_count are
NORMALIZED-layer names, not raw TikTok names anywhere in the system;
fed to ingestion they yield a PARTIAL metric of zeros. Kept
verbatim: id "7512345678901234567", stats 1000/120/15/8/20.

cases.json therefore records, per platform, the truth its OWNING
parser produces from the canonical fixture — never a zero/PARTIAL
golden. The remaining unification step belongs to the script's
owner (path A): teach facebook_raw()/tiktok_raw()/normalize() the
real raw shapes above, regenerate, and in that same merge move the
script-side expectations that still cite builder numbers
(facebook 12000/800/120/45, tiktok 50000/4200/310/190/260) to the
canonical ones. Until then this test pins each contract explicitly.

Run from repo root:  python -m pytest tests/test_fix_golden_unified_source.py -q
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = REPO_ROOT / "data" / "golden"
CASES_FILE = GOLDEN_DIR / "cases.json"
BUILDER_PLATFORMS = ("youtube",)
ALL_PLATFORMS = ("facebook", "tiktok", "youtube")


def _load_collect_module():
    spec = importlib.util.spec_from_file_location(
        "collect_sample_data", REPO_ROOT / "scripts" / "collect_sample_data.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


collect = _load_collect_module()


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _case(platform: str) -> dict:
    return next(c for c in _read_json(CASES_FILE) if c["platform"] == platform)


# --- cases.json lives in the canonical tree ---------------------------------
def test_cases_json_exists_in_canonical_tree():
    assert CASES_FILE.is_file(), (
        "data/golden/cases.json missing — build_golden_cases() points "
        "into data/golden/, so the cases file belongs there, not only "
        "in data/sample/golden/")


def test_cases_cover_all_three_platforms():
    cases = _read_json(CASES_FILE)
    assert [c["platform"] for c in cases] == list(ALL_PLATFORMS)
    assert [c["id"] for c in cases] == [f"golden-{p}-001" for p in ALL_PLATFORMS]


def test_every_case_fixture_path_exists_and_stays_in_canonical_tree():
    for case in _read_json(CASES_FILE):
        rel = case["raw_fixture_path"]
        assert rel.startswith("data/golden/"), rel
        assert (REPO_ROOT / rel).is_file(), f"missing golden fixture: {rel}"


# --- Youtube: builder-canonical ----------------------------------------------
@pytest.mark.parametrize("platform", BUILDER_PLATFORMS)
def test_on_disk_golden_fixture_equals_raw_builder(platform):
    fixture_file = GOLDEN_DIR / platform / "video_list.json"
    assert fixture_file.is_file(), f"missing {fixture_file}"
    assert _read_json(fixture_file) == collect.RAW_BUILDERS[platform](), (
        f"data/golden/{platform}/video_list.json has drifted from "
        f"RAW_BUILDERS[{platform!r}] — regenerate it from "
        "scripts/collect_sample_data.py instead of hand-editing")


@pytest.mark.parametrize("platform", BUILDER_PLATFORMS)
def test_builder_platform_case_reproduces_from_canonical_fixture(platform):
    case = _case(platform)
    raw = _read_json(REPO_ROOT / case["raw_fixture_path"])
    metrics = collect.normalize(platform, raw)
    assert len(metrics) == case["expected_video_count"]
    assert metrics[0] == case["expected_first_metric"]


# --- Facebook: the connector contract, kept verbatim (systems-arch) ---------
def test_facebook_golden_fixture_keeps_the_connector_contract():
    raw = _read_json(GOLDEN_DIR / "facebook" / "video_list.json")
    video = raw["data"][0]
    # VIDEO_FIELDS the connector requests and parses directly.
    assert video["id"] == "987"
    assert video["title"] == "T"
    assert video["description"] == "D"
    assert video["permalink_url"] == "https://fb.watch/987"
    assert video["length"] == 60               # -> duration_seconds
    assert video["views"] == 100               # fallback source for views
    assert video["likes"]["summary"]["total_count"] == 5
    assert video["comments"]["summary"]["total_count"] == 2
    assert video["shares"]["count"] == 1
    # INSIGHT_METRICS, as a top-level list: the test double for the
    # separate {video_id}/video_insights endpoint. post_* names are
    # post-level metrics; total_* would be a different (Page-level)
    # metric family and parses to views=0 on the real connector.
    insights = {d["name"]: d["values"][0]["value"] for d in raw["insights"]}
    assert insights == {
        "post_video_views": 100,
        "post_video_view_time": 1234,          # TOTAL watch time, seconds
        "post_impressions": 500,
    }


def test_facebook_case_records_the_connector_parsed_truth():
    case = _case("facebook")
    assert case["raw_fixture_path"] == "data/golden/facebook/video_list.json"
    assert case["expected_video_count"] == 1
    metric = case["expected_first_metric"]
    # The numbers the real connector parser produces from the fixture
    # above (systems-arch, measured): never views=0 / quality PARTIAL
    # in a golden fixture.
    assert metric["platform_video_id"] == "987"
    assert metric["views"] == 100
    assert metric["likes"] == 5
    assert metric["comments"] == 2
    assert metric["shares"] == 1
    assert metric["watch_time_seconds"] == 1234
    assert metric["quality"] == "ok"


# --- Tiktok: the real-API contract, kept verbatim (algo-ml) ------------------
def test_tiktok_golden_fixture_keeps_the_real_api_shape():
    raw = _read_json(GOLDEN_DIR / "tiktok" / "video_list.json")
    # Pagination shape of /video/list/ that list_videos walks.
    assert raw["data"]["cursor"] == ""
    assert raw["data"]["has_more"] is False
    video = raw["data"]["videos"][0]
    # The exact field set the connector requests from the API;
    # map_video uses desc as fallback for title AND description, and
    # cover_image_url / share_url feed thumbnail_url / url.
    assert video["id"] == "7512345678901234567"   # numeric-string, as served
    assert video["title"] == "Meo tam su dem khuya"
    assert video["desc"] == "meo tam su dem khuya"
    assert video["cover_image_url"] == "https://example.com/cover.jpg"
    assert video["share_url"] == (
        "https://www.tiktok.com/@test/video/7512345678901234567")
    assert isinstance(video["create_time"], int)  # epoch, not ISO text
    assert video["duration"] == 32
    # Counters nested in `stats` under RAW TikTok names — the names
    # map_metric and ingestion RAW_FIELD_MAP actually read. The flat
    # view_count/like_count spelling is a normalized-layer name and
    # ingests to a PARTIAL metric of zeros.
    assert video["stats"] == {
        "play_count": 1000,
        "digg_count": 120,
        "comment_count": 15,
        "share_count": 8,
        "collect_count": 20,
    }


def test_tiktok_case_records_the_parsed_truth():
    case = _case("tiktok")
    assert case["raw_fixture_path"] == "data/golden/tiktok/video_list.json"
    assert case["expected_video_count"] == 1
    metric = case["expected_first_metric"]
    # Pinned by tests/test_algo_ml_m2.py::test_tiktok_list_and_stats_mapping
    # (algo-ml, measured on map_video/map_metric): views 1000, likes
    # 120, saves 20, engagement_rate (120+15+8)/1000 = 0.143.
    assert metric["platform_video_id"] == "7512345678901234567"
    assert metric["views"] == 1000
    assert metric["likes"] == 120
    assert metric["comments"] == 15
    assert metric["shares"] == 8
    assert metric["saves"] == 20
    assert metric["quality"] == "ok"
