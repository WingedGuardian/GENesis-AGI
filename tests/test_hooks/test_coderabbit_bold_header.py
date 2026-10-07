"""CodeRabbit's bold severity header (#2992).

Through 2026-10-05 CodeRabbit opened each inline finding with italic fields
(`_⚠️ Potential issue_ | _🟠 Major_`); from 2026-10-06 with bold ones
(`**🩺 Stability & Availability** | **🟠 Major** | **⚡ Quick win**`). The shared
field matcher read only the italic form, so every new finding fell to the
unknown-format canary: a Major never reached the always-fix floor, and round
counting read every Trivial as an unreadable (so counted) header.

The headers below are copied from real comments of 2026-10-06. What they pin:

1. a bold header reads its level exactly as an italic one does;
2. a LONE bold line is not a header: it is how CodeRabbit writes a finding's
   TITLE, and reading it as one would make every title an unreadable header;
3. the title extractor skips the bold header line;
4. the italic reading is unchanged (the full-corpus replay is in the PR body;
   the existing italic tests stay green).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "hooks"))

import review_findings as rf  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "git_push_guard_bold", _ROOT / "scripts" / "hooks" / "git_push_guard.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

RABBIT = "coderabbitai[bot]"

# Real headers, 2026-10-06 (PRs #2914, #2900, #2949).
MAJOR = "**🩺 Stability & Availability** | **🟠 Major** | **⚡ Quick win**"
MINOR = "**🎯 Functional Correctness** | **🟡 Minor** | **⚡ Quick win**"
MINOR_EXTRA_FIELD = (
    "**🔒 Security & Privacy** | **🛡️ Detected with Advanced Tier** | **🟡 Minor** | "
    "**⚡ Quick win**"
)
# Not yet observed in bold; built from the same field shape and level ladder.
CRITICAL = "**🔒 Security & Privacy** | **🔴 Critical** | **🔨 Heavy lift**"
TRIVIAL = "**📐 Maintainability & Code Quality** | **🔵 Trivial** | **⚡ Quick win**"


def _comment(header: str, title: str = "Bound progress reporting separately.") -> str:
    return f"{header}\n\n**{title}**\n\nThe body explains the problem.\n"


@pytest.mark.parametrize(
    ("header", "level"),
    [
        (MAJOR, "major"),
        (MINOR, "minor"),
        (MINOR_EXTRA_FIELD, "minor"),
        (CRITICAL, "critical"),
        (TRIVIAL, "trivial"),
    ],
    ids=["major", "minor", "extra-field", "critical", "trivial"],
)
def test_a_bold_header_reads_its_level(header, level):
    assert rf.cr_severity(_comment(header)) == (level, True)
    assert guard._cr_severity(_comment(header)) == (level, True)


def test_a_bold_and_an_italic_header_read_the_same():
    italic = "_⚠️ Potential issue_ | _🟠 Major_"
    assert rf.cr_severity(_comment(italic)) == rf.cr_severity(_comment(MAJOR)) == ("major", True)


def test_a_lone_bold_line_is_a_title_not_a_header():
    assert rf.cr_header_fields("**Bound progress reporting separately.**") is None
    assert rf.cr_severity("**Critical**\n\nprose") == (None, False)


def test_a_bold_header_with_an_unreadable_field_is_a_canary():
    """Header-shaped, but one field is not a complete span: seen, level unknown."""
    assert rf.cr_severity("**🎯 Functional Correctness** | 🟠 Major **") == (None, True)


def test_a_minor_quoting_bold_critical_stays_minor():
    """The 152-token precision case, bold edition: only the header decides."""
    body = _comment(MINOR) + "\n```python\n**Critical** | **Major**\nMAX_CRITICAL_ERRORS = 3\n```\n"
    assert rf.cr_severity(body) == ("minor", True)
    assert [guard._cr_severity(p)[0] for p in guard._cr_findings(body)] == ["minor"]


# ── round counting (`is_finding`) ───────────────────────────────────────────


def test_a_bold_trivial_is_informational_and_its_title_does_not_count():
    """The trap: a title line read as an unreadable header would count the round."""
    assert rf.is_finding(RABBIT, _comment(TRIVIAL)) is False


@pytest.mark.parametrize("header", [MAJOR, MINOR, CRITICAL])
def test_a_bold_finding_counts(header):
    assert rf.is_finding(RABBIT, _comment(header)) is True


def test_a_bold_trivial_ahead_of_a_bold_major_still_counts():
    bundled = _comment(TRIVIAL) + "\n---\n\n" + _comment(MAJOR)
    assert rf.is_finding(RABBIT, bundled) is True


# ── the merge gate's readers ────────────────────────────────────────────────


def test_bundled_bold_findings_are_split_and_each_read():
    body = _comment(MINOR, "First.") + "\n---\n\n" + _comment(MAJOR, "Second.")
    assert [guard._cr_severity(p)[0] for p in guard._cr_findings(body)] == ["minor", "major"]


def test_the_title_is_the_bold_line_under_the_bold_header():
    assert guard._coderabbit_title(_comment(MAJOR, "Raise the skip ceiling to 104.")) == (
        "Raise the skip ceiling to 104."
    )


def test_the_title_under_an_italic_header_is_unchanged():
    italic = "_⚠️ Potential issue_ | _🟠 Major_"
    assert guard._coderabbit_title(_comment(italic, "Same title.")) == "Same title."


@pytest.mark.parametrize(
    ("text", "level"),
    [
        (f"`a.py` (L1-L2): {MAJOR} Some prose", "major"),
        (f"{MINOR}", "minor"),
        ("`a.py`: _⚠️ Potential issue_ | _🟠 Major_ | trailing", "major"),
    ],
    ids=["bold-with-prose", "bold-alone", "italic-unchanged"],
)
def test_the_review_body_reader_reads_bold_fields(text, level):
    assert guard._cr_severity_inline(text) == (level, True)


def test_the_review_body_reader_ignores_a_lone_bold_chunk():
    """No `|`, so a bold run in prose is not a field there either."""
    assert guard._cr_severity_inline("see **Critical** above")[0] is None
    assert guard._cr_severity_inline("**Critical**")[0] is None


def test_a_bold_title_holding_a_pipe_is_still_the_title():
    """Only the FIRST line is skipped as the header; `str | None` is a real title shape."""
    body = _comment(MAJOR, "Annotate the field as `str | None`.")
    assert guard._coderabbit_title(body) == "Annotate the field as `str | None`."


@pytest.mark.parametrize(
    "line",
    ["_a_ | _b_", "_a | b_", "_|_", "_a_", "plain | text", "_a | b**", "| _a_ | 3 |", ""],
)
def test_the_italic_shape_reads_exactly_as_the_removed_regex(line):
    """The regex this replaced was ITALIC-only (`^_.*\\|.*_$`); italic must not move."""
    import re

    removed = re.compile(r"^_.*\|.*_$")
    assert rf.cr_header_shaped(line) is bool(removed.match(line))


@pytest.mark.parametrize(
    ("line", "shaped"),
    [
        ("**a** | **b**", True),
        ("**🎯 Functional Correctness** | 🟠 Major **", True),
        ("**a | b**", False),
        ("**Prefer `str | None` over Optional.**", False),
        ("**Fix the `a | b` split.**", False),
        ("**a**", False),
        ("| **a** | 3 |", False),
    ],
)
def test_a_bold_line_is_header_shaped_only_when_its_first_field_is_bold(line, shaped):
    """GLM P3: a bold TITLE holding a pipe opens and closes with `**` as well."""
    assert rf.cr_header_shaped(line) is shaped


def test_a_bold_trivial_whose_title_holds_a_pipe_does_not_count():
    """GLM P3 reproduction: the title is not an unreadable header."""
    body = _comment(TRIVIAL, "Prefer `str | None` over Optional.")
    assert rf.is_finding(RABBIT, body) is False


def test_a_headerless_finding_keeps_its_piped_bold_title():
    """GLM P3 reproduction: the first line is the title when it is not a header."""
    body = "**Fix the `a | b` split.**\n\n**Second bold line.**\n\nprose\n"
    assert guard._coderabbit_title(body) == "Fix the `a | b` split."
