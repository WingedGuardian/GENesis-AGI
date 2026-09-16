"""A plan's bookmark title must survive YAML frontmatter.

`plan_bookmark_hook` names a bookmark by the plan's first markdown heading, and
that name is what `/unshelve` searches by keyword. The extraction scanned the
first ten lines of the file, which a plan carrying frontmatter pushes its
heading past — so the title came back empty, the bookmark became unfindable,
and nothing raised or logged. MEASURED 2026-09-15 against a real headered plan
in `~/.claude/plans/`: the extracted title was ``''``.

Driven through `_extract_plan_info` with a real file on disk rather than
through the hook's stdin protocol: the defect is in what that function reads,
and the surrounding protocol adds nothing the assertion needs.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "plan_bookmark_hook.py"


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("plan_bookmark_hook", _HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _info(hook, tmp_path: Path, body: str) -> tuple[str, str]:
    """Write a plan and extract its (path, title).

    The file MUST live under a `.claude/plans/` path: `_extract_plan_info`
    only recognises a plan path matching that shape, and anything else falls
    through to the newest file in the REAL `~/.claude/plans/`. That fallback
    made an earlier version of this test pass by reading the developer's own
    plan file — green, hermetic-looking, and proving nothing.
    """
    plans = tmp_path / ".claude" / "plans"
    plans.mkdir(parents=True, exist_ok=True)
    plan = plans / "some-plan.md"
    plan.write_text(body)
    path, title = hook._extract_plan_info(
        {"tool_input": {"path": str(plan)}, "tool_output": {}},
    )
    assert path == str(plan), (
        f"the harness did not reach its own fixture (got {path!r}) — any "
        "assertion below would be about some other file"
    )
    return path, title


HEADER = """\
---
plan: some-plan
status: active
updated: 2026-09-15
pinned:
  main: 71e91e148ab3
  prs: [1932, 2033]
decisions: [828d7458b5dd4785]
ledger: [a9db0a3dac0a4dbfa8ca6d9221c70cac]
issues: [2052, 2060]
binds: "the drain triad ships first."
prevents: "a second findings store."
---
"""


def test_a_headered_plan_still_yields_its_title(hook, tmp_path):
    """The documented header is 13 lines — past the old scan window entirely."""
    _, title = _info(hook, tmp_path, HEADER + "\n# PR-flow work — build plan\n\nbody\n")
    assert title == "PR-flow work — build plan", (
        "the frontmatter pushed the heading out of the scan window, so the "
        f"bookmark is named {title!r} and cannot be found by keyword"
    )


def test_a_plain_plan_is_unaffected(hook, tmp_path):
    """Control: the no-frontmatter path must behave exactly as before."""
    _, title = _info(hook, tmp_path, "# Ordinary plan\n\nbody\n")
    assert title == "Ordinary plan"


def test_a_leading_thematic_break_is_not_frontmatter(hook, tmp_path):
    """Control for the fence detection.

    A lone `---` with no closing partner is a horizontal rule, not an open
    frontmatter block. Treating it as one would skip real content looking for
    a fence that never arrives.

    The heading sits IMMEDIATELY after the rule on purpose. With a blank line
    between them the test does not discriminate — a mutant that treats the
    next line as the closing fence slices off the blank and still finds the
    heading, which is exactly how an earlier version of this fixture let that
    mutation SURVIVE.
    """
    _, title = _info(hook, tmp_path, "---\n# After a rule\n\nbody\n")
    assert title == "After a rule"


def test_a_heading_far_below_the_header_is_not_the_title(hook, tmp_path):
    """The window stays bounded — a deep `#` is a section, not the title.

    Without this, "skip the frontmatter" is equally satisfied by scanning the
    whole file, which would name a 4,000-line plan after whatever section
    happens to come first.
    """
    body = HEADER + "\n" + ("filler\n" * 40) + "# Way down here\n"
    _, title = _info(hook, tmp_path, body)
    assert title == "", "a heading 40 lines below the header is not the document title"


def test_an_unterminated_fence_does_not_swallow_the_file(hook, tmp_path):
    """A `---` whose partner never comes must not blank the title.

    The closing-fence search is bounded; past that bound the text is scanned
    as written, so a malformed plan degrades to the old behaviour rather than
    to silence.
    """
    body = "---\n" + ("key: value\n" * 200) + "# Never reached anyway\n"
    plan_path, title = _info(hook, tmp_path, body)
    assert plan_path  # the file was found
    assert title == ""  # no heading within the window — but no crash, either
