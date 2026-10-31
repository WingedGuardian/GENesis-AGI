"""CodeRabbit's third inline header layout (2026-10-17 / 2026-10-25 on).

From 2026-10-17 every CodeRabbit inline finding opens with a marker comment,
`<!-- coderabbit-ai-generated:v1 -->`, ahead of its header; from 2026-10-25 the
header itself draws the level's icon as HTML and joins its fields with a middle
dot. MEASURED over all 1,162 top-level CodeRabbit inline comments in this repo:
91 from 2026-10-17 on read no level at all, among them 1 Critical and 21 Major,
so none of them reached the always-fix floor.

The headers below are copied from real comments (ids 4369771744, 4369908251,
4403628477, 4304181388). What they pin:

1. the icon header reads its level, through the shared reader and the gate;
2. the marker comment is skipped, so the bold-pipe header behind it reads too;
3. the finding's title is its bold line, not the header or the comment;
4. a malformed icon, a comment carrying text, and a lone bold title holding a
   middle dot are not read as a level;
5. the icon strip is linear on hostile input.
"""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "scripts" / "hooks"))

import review_findings as rf  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "git_push_guard_icon", _ROOT / "scripts" / "hooks" / "git_push_guard.py"
)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

RABBIT = "coderabbitai[bot]"
MARKER = "<!-- coderabbit-ai-generated:v1 -->"
_ICON = (
    '<picture><img src="https://storage.googleapis.com/coderabbit_public_assets/'
    'review-comment-icons-v1/{icon}-16.svg" width="16" height="16" align="absmiddle" '
    'alt=""></picture>'
)
MAJOR = f"**{_ICON.format(icon='major')} Major** · **Functional Correctness** · **Quick win**"
MINOR = f"**{_ICON.format(icon='amber')} Minor** · **Security & Privacy** · **Quick win**"
CRITICAL = (
    f"**{_ICON.format(icon='critical')} Critical** · **Data Integrity & Integration** · "
    "**Quick win**"
)
# Not yet observed in this layout; built from the same shape and level ladder.
TRIVIAL = f"**{_ICON.format(icon='trivial')} Trivial** · **Maintainability** · **Quick win**"
# 2026-10-17 to 10-24: the marker comment ahead of the bold-pipe header.
PIPE_MAJOR = "**🔒 Security & Privacy** | **🟠 Major** | **⚡ Quick win**"


def _comment(header: str, title: str = "Make terminal input cancellable on shutdown.") -> str:
    return f"{MARKER}\n\n{header}\n\n**{title}**\n\nThe body explains the problem.\n"


@pytest.mark.parametrize(
    ("header", "level"),
    [(MAJOR, "major"), (MINOR, "minor"), (CRITICAL, "critical"), (TRIVIAL, "trivial")],
    ids=["major", "minor", "critical", "trivial"],
)
def test_an_icon_header_reads_its_level(header, level):
    assert rf.cr_severity(_comment(header)) == (level, True)
    assert guard._cr_severity(_comment(header)) == (level, True)


def test_the_marker_comment_ahead_of_a_pipe_header_is_skipped():
    assert rf.cr_severity(_comment(PIPE_MAJOR)) == ("major", True)


@pytest.mark.parametrize("header", [MAJOR, CRITICAL, PIPE_MAJOR], ids=["major", "critical", "pipe"])
def test_a_blocking_finding_in_the_new_layouts_opens_a_round(header):
    assert rf.is_finding(RABBIT, _comment(header))


def test_an_icon_trivial_is_informational():
    assert not rf.is_finding(RABBIT, _comment(TRIVIAL))


@pytest.mark.parametrize("header", [MAJOR, PIPE_MAJOR], ids=["icon", "pipe"])
def test_the_title_is_the_bold_line_under_the_header(header):
    body = _comment(header)
    (segment,) = guard._cr_findings(body)
    assert guard._coderabbit_title(segment) == "Make terminal input cancellable on shutdown."


def test_an_unclosed_icon_never_reads_a_level_it_does_not_name():
    """Left in place, the span either overflows the field bound (no level) or
    leaves the level word last (the true level), never another one."""
    long_src = MAJOR.replace("</picture>", "")
    assert rf.cr_severity(_comment(long_src))[0] is None
    short_src = '**<picture><img src="x"> Major** · **Cat** · **Quick win**'
    assert rf.cr_severity(_comment(short_src)) == ("major", True)


def test_a_pipe_header_whose_field_holds_a_middle_dot_reads_as_before():
    """Round 1 on #3818 (Kimi): `|` splits whenever present, so a `·` inside a
    field cannot demote a real level."""
    bold = "**🔒 Security · Privacy** | **🟠 Major** | **⚡ Quick win**"
    italic = "_Security · Privacy_ | _🟠 Major_ | _⚡ Quick win_"
    assert rf.cr_severity(_comment(bold)) == ("major", True)
    assert rf.cr_severity(_comment(italic)) == ("major", True)


@pytest.mark.parametrize("line", ["_init_ · _b Critical_", "**Note** · **b Critical**"])
def test_a_two_field_middle_dot_line_is_not_a_header(line):
    """Round 1 on #3818 (Kimi): a `·` split needs the three fields every real
    header has, so prose cannot read as a level."""
    assert rf.cr_severity(f"{MARKER}\n{line}\n") == (None, False)


def test_only_coderabbits_marker_is_skipped():
    body = f"<!-- quoted -->\n{MAJOR}\n"
    assert rf.cr_severity(body) == (None, False)


def test_a_comment_line_carrying_text_is_not_skipped():
    body = f"{MARKER} note\n\n{MAJOR}\n"
    assert rf.cr_severity(body) == (None, False)


def test_the_marker_alone_is_a_marker_line():
    assert rf.is_cr_marker_line(MARKER)


@pytest.mark.parametrize(
    "line",
    [
        "<!-- a -->",
        f"{MARKER} b",
        f"{MARKER}<!-- b -->",
        "<!-- coderabbit-",
        "-->",
    ],
)
def test_anything_else_is_not_a_marker_line(line):
    assert not rf.is_cr_marker_line(line)


def test_a_lone_bold_title_holding_a_middle_dot_is_not_a_header():
    assert rf.cr_header_fields("**Keep a · b in step.**") is None
    assert rf.cr_severity(_comment(MAJOR, "Keep a · b in step.")) == ("major", True)


@pytest.mark.parametrize(
    "hostile",
    [
        "**" + "<picture<img" * 20_000 + " Major** · **x**",
        "<" * 65_000,
        "<img>" * 13_000 + "<picture>",
    ],
    ids=["unclosed-spans", "bare-angles", "many-tags"],
)
def test_the_icon_strip_is_linear_on_hostile_input(hostile):
    start = time.monotonic()
    rf.cr_severity(hostile)
    rf.cr_header_shaped(hostile)
    # Generous for a loaded runner; a quadratic scan of these inputs takes seconds.
    assert time.monotonic() - start < 0.25
