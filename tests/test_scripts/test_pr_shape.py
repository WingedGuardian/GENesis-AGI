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
    if count is not None:
        return [f"@@ -{start},{count} +{start},{count} @@", *lines]
    old = sum(1 for ln in lines if not ln or ln[0] in "- ")
    new = sum(1 for ln in lines if not ln or ln[0] in "+ ")
    return [f"@@ -{start},{old} +{start},{new} @@", *lines]


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


def test_prefixed_lines_inside_hunk_are_content():
    # `+++counter` and a `---` YAML separator inside a hunk are content, not
    # headers — counted, and the hunk keeps consuming until its counts run out.
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-a\n"
        "+++counter\n"
        " b\n"
        "@@ -5,3 +5,2 @@\n"
        "-old\n"
        "--- yaml separator\n"
        "+x\n"
        " c\n"
        "@@ -9,0 +9,1 @@\n"
        "+after\n"
    )
    result = ps.count_diff(diff)
    assert result["excluded"] == {}
    # added: ++counter, x, after; removed: a, old, -- yaml separator
    assert result["counted"] == 6
    assert result["by_file"] == {"x.py": 6}


def test_header_count_mismatch_unparseable():
    # Fewer body lines than the header declares: counts remain at the next
    # `diff --git` / EOF.
    truncated = _diff("x.py", ["@@ -1,5 +1,5 @@", "+a = 1"])
    assert ps.count_diff(truncated)["excluded"] == {"x.py": "unparseable"}
    # More body lines than declared: a count goes negative.
    overflow = _diff("x.py", ["@@ -1,1 +1,1 @@", "+a = 1", "+b = 2"])
    assert ps.count_diff(overflow)["excluded"] == {"x.py": "unparseable"}


def test_empty_context_line_tolerated():
    diff = _diff("x.py", ["@@ -1,2 +1,3 @@", "", "+x = 1", " y = 2"])
    result = ps.count_diff(diff)
    assert result["excluded"] == {}
    assert result["counted"] == 1


def test_quoted_path_with_space_excluded_as_prose():
    diff = (
        'diff --git "a/docs/my page.md" "b/docs/my page.md"\n'
        '--- "a/docs/my page.md"\n'
        '+++ "b/docs/my page.md"\n'
        "@@ -0,0 +1,1 @@\n"
        "+# Heading\n"
    )
    result = ps.count_diff(diff)
    assert result["excluded"] == {"docs/my page.md": "prose"}


def test_quoted_octal_path_decoded():
    diff = (
        'diff --git "a/src/caf\\303\\251.py" "b/src/caf\\303\\251.py"\n'
        '--- "a/src/caf\\303\\251.py"\n'
        '+++ "b/src/caf\\303\\251.py"\n'
        "@@ -0,0 +1,1 @@\n"
        "+x = 1\n"
    )
    result = ps.count_diff(diff)
    assert result["by_file"] == {"src/café.py": 1}


def test_deleted_file_keyed_by_old_path():
    diff = (
        "diff --git a/gone.py b/gone.py\n"
        "deleted file mode 100644\n"
        "--- a/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,2 +0,0 @@\n"
        "-a = 1\n"
        "-b = 2\n"
    )
    result = ps.count_diff(diff)
    assert result["excluded"] == {}
    assert result["by_file"] == {"gone.py": 2}


def test_unicode_separator_inside_line_stays_one_line():
    diff = _diff("x.py", ["@@ -0,0 +1,1 @@", "+x = 1  y = 2"])
    result = ps.count_diff(diff)
    assert result["counted"] == 1


def test_parse_shape_empty_then_nonempty():
    assert ps.parse_shape("Shape:\nShape: real explanation") == "real explanation"


def test_parse_shape_fenced_line_ignored_via_readable_body():
    assert ps.parse_shape("```\nShape: hidden\n```\nShape: shown") == "shown"
    assert ps.parse_shape("```\nShape: hidden\n```") is None


def test_shell_test_scripts_excluded():
    diff = _diff("scripts/test_cc_cli.sh", _hunk(["+echo hi"])) + _diff(
        "scripts/spike_caveat_fixes_test.sh", _hunk(["+echo hi"])
    )
    result = ps.count_diff(diff)
    assert result["excluded"] == {
        "scripts/test_cc_cli.sh": "test",
        "scripts/spike_caveat_fixes_test.sh": "test",
    }


def test_txt_counts_as_code_now():
    diff = _diff("requirements.txt", _hunk(["+flask==3.0"])) + _diff(
        "config/az-pip-constraints.txt", _hunk(["+urllib3>=2.7"])
    )
    result = ps.count_diff(diff)
    assert result["excluded"] == {}
    assert result["counted"] == 2


def test_other_prose_spellings_excluded():
    diff = _diff("docs/guide.markdown", _hunk(["+# T"])) + _diff(
        "COPYING", _hunk(["+text"])
    )
    result = ps.count_diff(diff)
    assert result["excluded"] == {"docs/guide.markdown": "prose", "COPYING": "prose"}


def test_dot_test_js_is_a_test():
    diff = _diff("web/foo.test.js", _hunk(["+it('x')"]))
    assert ps.count_diff(diff)["excluded"] == {"web/foo.test.js": "test"}


def test_change_lines_after_completed_hunk_unparseable():
    diff = (
        "diff --git a/x.py b/x.py\n"
        "--- a/x.py\n"
        "+++ b/x.py\n"
        "@@ -0,0 +1 @@\n"
        "+a = 1\n"
        "+b = 2\n"
    )
    assert ps.count_diff(diff)["excluded"] == {"x.py": "unparseable"}


def test_unquote_decodes_c_escapes():
    for esc, byte in (("a", 7), ("b", 8), ("v", 11), ("f", 12), ("r", 13), ("n", 10), ("t", 9)):
        out = ps._unquote(f'"a/x\\{esc}y"')
        assert out == f"a/x{chr(byte)}y", (esc, out)


def test_load_failure_raises_runtime_error(monkeypatch):
    def boom(filename, name):
        raise RuntimeError(f"cannot load {filename}")

    monkeypatch.setattr(ps, "_load_sibling", boom)
    import pytest

    with pytest.raises(RuntimeError, match="cannot load review_scope.py"):
        ps.count_diff(_diff("x.py", _hunk(["+a = 1"])))
    with pytest.raises(RuntimeError, match="cannot load check_cc_pin_receipts.py"):
        ps.parse_shape("Shape: x")


def test_parse_shape_fence_mismatch_does_not_close():
    body = "```\n~~~\nShape: hidden\n```\nShape: shown"
    assert ps.parse_shape(body) == "shown"


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
