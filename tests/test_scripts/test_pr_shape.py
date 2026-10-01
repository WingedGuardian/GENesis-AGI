"""Tests for scripts/pr_shape.py — the PR size counter."""

from __future__ import annotations

import importlib.util
import random
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent.parent / "scripts" / "pr_shape.py"
_spec = importlib.util.spec_from_file_location("pr_shape", _MODULE_PATH)
assert _spec and _spec.loader
ps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ps)


def _diff(path: str, body: list[str], old: str | None = None) -> str:
    lines = [f"diff --git a/{old or path} b/{path}", f"--- a/{old or path}", f"+++ b/{path}"]
    lines.extend(body)
    return "\n".join(lines) + "\n"


def _hunk(lines: list[str], start: int = 1, count: int | None = None) -> list[str]:
    n = count if count is not None else len(lines)
    return [f"@@ -{start},{n} +{start},{n} @@", *lines]


def test_three_added_lines_count_three():
    diff = _diff("x.py", _hunk(["+a = 1", "+b = 2", "+c = 3"]))
    result = ps.count_diff(diff)
    assert result["counted"] == 3
    assert result["by_file"] == {"x.py": 3}
    assert result["band"] == "ok"


def test_blank_and_comment_lines_not_counted():
    diff = _diff(
        "x.py",
        _hunk(["+a = 1", "+", "+   ", "+# a comment", "-# another comment", "-", "+b = 2"]),
    )
    result = ps.count_diff(diff)
    assert result["counted"] == 2


def test_slash_comments_not_counted_in_js():
    diff = _diff("x.js", _hunk(["+const a = 1;", "+// comment", "+const b = 2;"]))
    assert ps.count_diff(diff)["counted"] == 2


def test_excluded_files_carry_reasons():
    diff = (
        _diff("tests/test_x.py", _hunk(["+assert True"]))
        + _diff("docs/a.md", _hunk(["+# Heading"]))
        + _diff("changelog.d/x.md", _hunk(["+- entry"]))
        + "diff --git a/blob.bin b/blob.bin\nBinary files a/blob.bin and b/blob.bin differ\n"
    )
    result = ps.count_diff(diff)
    assert result["counted"] == 0
    assert result["excluded"] == {
        "tests/test_x.py": "test",
        "docs/a.md": "prose",
        "changelog.d/x.md": "changelog",
        "blob.bin": "binary",
    }


def test_changelog_md_excluded():
    diff = _diff("CHANGELOG.md", _hunk(["+- entry"]))
    assert ps.count_diff(diff)["excluded"] == {"CHANGELOG.md": "changelog"}


def test_moved_block_counts_once():
    block = [f"line_{i}" for i in range(10)]
    diff = _diff("a.py", _hunk([f"-{line}" for line in block])) + _diff(
        "b.py", _hunk([f"+{line}" for line in block])
    )
    result = ps.count_diff(diff)
    assert result["counted"] == 10
    assert result["moved"] == 10


def test_pure_rename_counts_zero():
    diff = (
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\n"
        "rename from old.py\n"
        "rename to new.py\n"
    )
    result = ps.count_diff(diff)
    assert result["counted"] == 0
    assert result["moved"] == 0


def _diff_with_adds(path: str, n: int) -> str:
    return _diff(path, _hunk([f"+x{i} = 1" for i in range(n)]))


def test_band_boundaries():
    assert ps.count_diff(_diff_with_adds("x.py", 499))["band"] == "ok"
    assert ps.count_diff(_diff_with_adds("x.py", 500))["band"] == "shape"
    assert ps.count_diff(_diff_with_adds("x.py", 1000))["band"] == "shape"
    assert ps.count_diff(_diff_with_adds("x.py", 1001))["band"] == "override"


def test_parse_shape_returns_text():
    assert ps.parse_shape("Body\nShape: one mechanism, the parsers move\nMore") == (
        "one mechanism, the parsers move"
    )


def test_parse_shape_inside_fence_returns_none():
    assert ps.parse_shape("```\nShape: hidden\n```") is None


def test_parse_shape_empty_returns_none():
    assert ps.parse_shape("Shape:") is None
    assert ps.parse_shape("Shape:   ") is None
    assert ps.parse_shape(None) is None
    assert ps.parse_shape("no shape line") is None


def test_garbage_does_not_raise():
    result = ps.count_diff("this is not a diff at all\n\x00\x01random\n@@@")
    assert result["counted"] == 0


def test_malformed_hunk_recorded_unparseable():
    diff = "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ nonsense @@\n+a = 1\n"
    result = ps.count_diff(diff)
    assert result["excluded"] == {"x.py": "unparseable"}


def test_random_diffs_never_raise_and_count_nonnegative():
    rng = random.Random(2737)
    fragments = [
        "diff --git a/f.py b/f.py",
        "--- a/f.py",
        "+++ b/f.py",
        "@@ -1,3 +1,3 @@",
        "+code",
        "-other",
        "+# comment",
        "+",
        " context",
        "garbage",
        "Binary files a/x b/x differ",
        "similarity index 100%",
        "@@ broken @@",
    ]
    for _ in range(200):
        diff = "\n".join(rng.choice(fragments) for _ in range(rng.randint(0, 40)))
        result = ps.count_diff(diff)
        assert result["counted"] >= 0
