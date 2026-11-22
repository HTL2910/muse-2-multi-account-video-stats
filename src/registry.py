` nào.** Quy ước nộp file chỉ biết ghi nội dung; bất kỳ khối FILE nào (kể cả để trống) cũng sẽ tái tạo lại file, đúng thứ hội đồng đã chốt xóa, và sẽ làm test guard dưới đây đỏ ngay. Vì vậy file mục tiêu được xử lý bằng lệnh xóa ở trên, sản phẩm nộp duy nhất là file test guard.

Kiểm chứng trước khi xóa, đúng phương án đã chốt:
- Đã đọc `docs/ARCHITECTURE.md` và `README.md` trên main: **không có bất kỳ tham chiếu nào** tới `registry`/`APPROACH_REGISTRY`/`register_approach` — phần "xóa tham chiếu trong docs" không có gì để xóa (các file `docs/hoi-dong-nghien-cuu/*.md` là bản ghi nghiên cứu lịch sử, không phải tài liệu sống trỏ tới module).
- File trên main đúng như mô tả ba vòng liền: `APPROACH_REGISTRY = {}` + `register_approach` không caller; các vòng audit trước đã grep xác nhận không module nào import.

Tự kiểm chứng trên VM với 3 trạng thái: (A) file đã xóa → **3 passed** + chạy lẻ PASS; (B) file còn tồn tại (trạng thái main hiện tại) → 3 test cùng fail; (C) file đã xóa nhưng còn một module import treo `from src.registry import ...` → đúng test tham chiếu fail. Guard khóa cả ba mặt: file không tồn tại, module không import được, và không code production nào (`src/`, `scripts/`) còn import hay nhắc hai tên công khai của stub — xóa xong mà sót tham chiếu là đỏ ngay thay vì vỡ runtime.

[[FILE tests/test_fix_registry_dead_code.py]]
"""Regression guard for fix_registry_dead_code.

src/registry.py was an empty stub — APPROACH_REGISTRY = {} and a
register_approach() that no module ever called: no approach was ever
registered and nothing imported it. Three council audits in a row
classified it as dead code that falsely implies a central discovery
mechanism, and the council locked the decision to DELETE it (not to
populate it). The deletion kept being skipped as the last step of
larger wiring fixes, so it now lands as its own cleanup change with
this guard:

- the file src/registry.py must NOT exist;
- the module src.registry must NOT be importable;
- no production code (src/, scripts/) may import it or reference its
  two public names — a dangling reference after deletion is a bug,
  not a reason to resurrect the stub.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

REGISTRY_FILE = REPO_ROOT / "src" / "registry.py"

_IMPORT_PATTERNS = [
    re.compile(r"(?m)^\s*import\s+src\.registry\b"),
    re.compile(r"(?m)^\s*from\s+src\.registry\s+import\b"),
    re.compile(r"(?m)^\s*from\s+src\s+import\s+[^\n#]*\bregistry\b"),
    re.compile(r"(?m)^\s*from\s+\.registry\s+import\b"),
    re.compile(r"(?m)^\s*from\s+\.\.registry\s+import\b"),
    re.compile(r"(?m)^\s*import\s+registry\b"),
    re.compile(r"(?m)^\s*from\s+registry\s+import\b"),
]
_PUBLIC_NAMES = ("APPROACH_REGISTRY", "register_approach")


def test_registry_file_is_deleted():
    assert not REGISTRY_FILE.exists(), (
        "src/registry.py is back — the council locked its deletion as "
        "dead code (empty APPROACH_REGISTRY, zero importers). Do not "
        "resurrect the stub; if a real registry is ever needed it "
        "returns as a designed feature with its own approach.")


def test_registry_module_is_not_importable():
    assert "src.registry" not in sys.modules, (
        "src.registry was imported during this test session — "
        "something still depends on the deleted stub")
    assert importlib.util.find_spec("src.registry") is None


def test_no_production_code_references_registry():
    offenders = []
    for base in (REPO_ROOT / "src", REPO_ROOT / "scripts"):
        for path in sorted(base.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            for pattern in _IMPORT_PATTERNS:
                if pattern.search(source):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: import")
            for name in _PUBLIC_NAMES:
                if name in source:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}: {name}")
    assert not offenders, (
        "dangling references to the deleted src/registry.py: "
        + "; ".join(offenders))


if __name__ == "__main__":
    test_registry_file_is_deleted()
    test_registry_module_is_not_importable()
    test_no_production_code_references_registry()
    print("ALL TESTS PASSED")
