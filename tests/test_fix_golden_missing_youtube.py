"""Regression tests for fix_golden_missing_youtube.

Canonical golden tree is ``data/golden/`` (locked structure). Before this
fix it held only facebook + tiktok; the YouTube fixture existed solely in
the duplicate ``data/sample/golden/`` tree, while every GoldenCase —
``scripts/collect_sample_data.build_golden_cases()`` and the cases file —
already pointed at ``data/golden/youtube/video_list.json``, a path that
did not exist. These tests pin the unified tree:

- all three platform fixtures exist under data/golden/ and are valid JSON
  with at least one video;
- every ``raw_fixture_path`` referenced by any GoldenCase source on the
  repo (build_golden_cases() and data/sample/golden/cases.json) exists;
- the YouTube fixture's content matches its golden case exactly
  (id / title / views / likes / comments), i.e. it is the same fixture
  that lived in data/sample/golden, promoted to the canonical location.

Run from repo root:  python -m pytest tests/test_fix_golden_missing_youtube.py -q
Stdlib only.
"""
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = REPO_ROOT / "data" / "golden"
PLATFORMS = ("facebook", "tiktok", "youtube")
GOLDEN_FILES = {p: GOLDEN_DIR / p / "video_list.json" for p in PLATFORMS}
SAMPLE_CASES_FILE = REPO_ROOT / "data" / "sample" / "golden" / "cases.json"
COLLECT_SCRIPT = REPO_ROOT / "scripts" / "collect_sample_data.py"


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _video_list(raw) -> list:
    """Extract the video list from any of the 3 platforms' raw shapes."""
    if isinstance(raw, dict):
        if isinstance(raw.get("items"), list):      # YouTube: items[]
            return raw["items"]
        data = raw.get("data")
        if isinstance(data, list):                   # Facebook: data[]
            return data
        if isinstance(data, dict) and isinstance(data.get("videos"), list):
            return data["videos"]                    # TikTok: data.videos[]
    return []


def _golden_cases_from_script() -> list[dict]:
    """GoldenCases declared by scripts/collect_sample_data.build_golden_cases()."""
    if not COLLECT_SCRIPT.exists():
        return []
    spec = importlib.util.spec_from_file_location("collect_sample_data", COLLECT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # stdlib-only script, no side effects on import
    return list(module.build_golden_cases())


def _golden_cases_from_sample_file() -> list[dict]:
    if not SAMPLE_CASES_FILE.exists():
        return []
    return list(_load_json(SAMPLE_CASES_FILE))


def _all_golden_cases() -> list[dict]:
    cases = _golden_cases_from_script() + _golden_cases_from_sample_file()
    assert cases, "no GoldenCase source found (script build_golden_cases / sample cases.json)"
    return cases


def test_all_three_golden_files_exist_and_are_valid():
    for platform, path in GOLDEN_FILES.items():
        assert path.exists(), f"missing canonical golden fixture: data/golden/{platform}/video_list.json"
        raw = _load_json(path)  # raises if not valid JSON
        videos = _video_list(raw)
        assert len(videos) >= 1, f"golden fixture for {platform} contains no video: {path}"


def test_every_golden_case_raw_fixture_path_exists():
    missing = []
    for case in _all_golden_cases():
        rel = case.get("raw_fixture_path", "")
        assert rel, f"GoldenCase {case.get('id')} has empty raw_fixture_path"
        if not (REPO_ROOT / rel).exists():
            missing.append(rel)
    assert not missing, f"GoldenCase raw_fixture_path(s) missing on repo: {missing}"


def test_golden_video_counts_match_cases():
    counts = {p: len(_video_list(_load_json(path))) for p, path in GOLDEN_FILES.items()}
    for case in _all_golden_cases():
        platform = case.get("platform")
        expected = case.get("expected_video_count")
        if platform in counts and expected:
            assert counts[platform] == expected, (
                f"{platform}: golden file holds {counts[platform]} video(s), "
                f"case {case.get('id')} expects {expected}")


def test_youtube_golden_matches_its_case_exactly():
    # The regression at the heart of this fix: YouTube at the canonical path.
    youtube_file = GOLDEN_FILES["youtube"]
    assert youtube_file.exists(), "data/golden/youtube/video_list.json is missing"
    raw = _load_json(youtube_file)
    assert raw.get("kind") == "youtube#videoListResponse"
    videos = _video_list(raw)
    assert len(videos) == 1
    item = videos[0]
    youtube_cases = [c for c in _all_golden_cases() if c.get("platform") == "youtube"]
    assert youtube_cases, "no YouTube GoldenCase declared"
    expected = youtube_cases[0].get("expected_first_metric") or {}
    if expected:
        assert item["id"] == expected["platform_video_id"]
        assert item["snippet"]["title"] == expected["title"]
        stats = item["statistics"]
        assert int(stats["viewCount"]) == expected["views"]
        assert int(stats["likeCount"]) == expected["likes"]
        assert int(stats["commentCount"]) == expected["comments"]
    # The fixture is the YouTube Data API v3 shape the connector parses:
    # duration is ISO-8601 and statistics are numeric strings.
    assert item["contentDetails"]["duration"].startswith("PT")
    for key in ("viewCount", "likeCount", "commentCount"):
        assert item["statistics"][key].isdigit(), f"statistics.{key} must be a numeric string"


if __name__ == "__main__":
    test_all_three_golden_files_exist_and_are_valid()
    test_every_golden_case_raw_fixture_path_exists()
    test_golden_video_counts_match_cases()
    test_youtube_golden_matches_its_case_exactly()
    print("ALL TESTS PASSED")
