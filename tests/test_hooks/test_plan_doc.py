"""The shared plan reader, and the two hooks that read plans through it.

`scripts/hooks/plan_doc.py` answers two questions for every hook that reads the
plan an ExitPlanMode is about: WHICH plan file, and WHICH PART of it is live.
Before it existed `adopt_first_gate` and `plan_bookmark_hook` each answered both,
and disagreed. Each test below pins one answer, and the hook-level tests drive
the REAL payload shape (`plan` + `planFilePath`, measured on 243 of 243 live
ExitPlanMode inputs) through each hook, so a hook that stops calling the reader
fails here rather than passing on a convenient shape.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_GATE = _SCRIPTS / "hooks" / "adopt_first_gate.py"
_BOOKMARK = _SCRIPTS / "plan_bookmark_hook.py"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def plan_doc():
    return _load("plan_doc_under_test", _SCRIPTS / "hooks" / "plan_doc.py")


@pytest.fixture(scope="module")
def bookmark():
    return _load("plan_bookmark_hook_under_test", _BOOKMARK)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway HOME, so no fallback can ever read the developer's real plans."""
    h = tmp_path / "home"
    (h / ".claude" / "plans").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(h))
    return h


def _write_plan(home: Path, name: str, text: str) -> Path:
    p = home / ".claude" / "plans" / name
    p.write_text(text, encoding="utf-8")
    return p


def _real_payload(path: Path, **extra) -> dict:
    """The shape Claude Code actually sends: `plan` serializes BEFORE `planFilePath`."""
    return {
        "tool_name": "ExitPlanMode",
        "tool_input": {"plan": path.read_text(encoding="utf-8"), "planFilePath": str(path)},
        **extra,
    }


# ── which plan ───────────────────────────────────────────────────────────────
def test_the_authoritative_field_wins_over_a_path_quoted_in_the_plan(plan_doc, home):
    other = _write_plan(home, "other.md", "# Other\n")
    mine = _write_plan(home, "mine.md", f"# Mine\n\nSee {other} for background.\n")
    assert plan_doc.plan_path(_real_payload(mine)) == str(mine)


def test_without_a_path_the_sessions_own_plan_is_found(plan_doc, home):
    _write_plan(home, "a-plan.md", "# A\n")
    own = _write_plan(home, "work-sess1234.md", "# Mine\n")
    assert plan_doc.plan_path({"session_id": "sess1234"}) == str(own)


def test_without_a_path_another_sessions_plan_is_never_guessed(plan_doc, home):
    _write_plan(home, "a.md", "# A\n")
    _write_plan(home, "b.md", "# B\n")
    assert plan_doc.plan_path({"session_id": "nomatch"}) == ""


def test_a_single_unambiguous_plan_is_the_safe_pick(plan_doc, home):
    only = _write_plan(home, "only.md", "# Only\n")
    assert plan_doc.plan_path({}) == str(only)


# ── which part ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "divider",
    [
        "## ═══ SUPERSEDED BELOW ═══",
        "# ═══ SUPERSEDED BELOW — resume block kept for history",
        "## ═══ SUPERSEDED BELOW ═══ (rev 1, kept for provenance)",
        "## SUPERSEDED BELOW",
    ],
)
def test_each_divider_spelling_in_real_plans_divides(plan_doc, divider):
    text = f"# Plan\n\nlive part\n\n{divider}\n\nold part\n"
    live = plan_doc.live_section(text)
    assert "live part" in live and "old part" not in live


def test_no_divider_means_the_whole_plan_is_live(plan_doc):
    text = "# Plan\n\nall of it\n"
    assert plan_doc.live_section(text) == text


def test_a_divider_shown_inside_a_fence_does_not_divide(plan_doc):
    text = "# Plan\n\n```\n## ═══ SUPERSEDED BELOW ═══\n```\n\nstill live\n"
    assert "still live" in plan_doc.live_section(text)


@pytest.mark.parametrize(
    "fenced",
    [
        "````\n```\n## SUPERSEDED BELOW\n```\n````",  # shorter inner fence is content
        "```\n```md\n## SUPERSEDED BELOW\n```",  # a fence line with an info string never closes
        "~~~\n```\n## SUPERSEDED BELOW\n```\n~~~",  # other character is content
        "```\n    ```\n## SUPERSEDED BELOW\n```",  # 4-space indent is content, not a delimiter
    ],
)
def test_a_divider_inside_a_nested_example_does_not_divide(plan_doc, fenced):
    text = f"# Plan\n\n{fenced}\n\nstill live\n"
    assert "still live" in plan_doc.live_section(text)


def test_a_longer_closing_fence_closes_and_a_later_divider_divides(plan_doc):
    text = "# Plan\n\n```\nexample\n`````\n\nlive\n\n## SUPERSEDED BELOW\n\nold\n"
    live = plan_doc.live_section(text)
    assert "live" in live and "old" not in live


def test_the_divider_phrase_in_prose_does_not_divide(plan_doc):
    text = "# Plan\n\nWe keep a SUPERSEDED BELOW divider here.\n\nstill live\n"
    assert "still live" in plan_doc.live_section(text)


def test_a_heading_that_mentions_the_phrase_is_not_the_divider(plan_doc):
    """A plan ABOUT the divider must not lose its whole body to its own title."""
    text = "# Plan: teach hooks the SUPERSEDED BELOW divider\n\nall of it is live\n"
    assert "all of it is live" in plan_doc.live_section(text)


def test_a_section_merely_labelled_superseded_is_not_the_divider(plan_doc):
    """Long plans keep superseded REVISIONS above the real divider, labelled as such."""
    text = "# Plan\n\n## ═══ (superseded) rev 2 ═══\n\nkept above\n"
    assert "kept above" in plan_doc.live_section(text)


# ── through the hooks ────────────────────────────────────────────────────────
def test_bookmark_records_the_plan_that_was_approved(bookmark, home):
    other = _write_plan(home, "other.md", "# Other plan\n")
    mine = _write_plan(home, "mine.md", f"# My plan\n\nBuilds on {other}.\n")
    path, title = bookmark._extract_plan_info(_real_payload(mine))
    assert (path, title) == (str(mine), "My plan")


@pytest.mark.parametrize("key", ["tool_response", "tool_output"])
def test_without_the_field_the_tools_returned_path_beats_one_quoted_in_the_plan(
    plan_doc, bookmark, home, key
):
    """An older payload with no planFilePath: the plan's prose serializes first,
    so a whole-payload scan would take the path it QUOTES, not the approved one."""
    other = _write_plan(home, "other.md", "# Other plan\n")
    mine = _write_plan(home, "mine.md", f"# My plan\n\nSee {other}.\n")
    payload = {
        "tool_name": "ExitPlanMode",
        "tool_input": {"plan": mine.read_text(encoding="utf-8")},
        # Claude Code's real ExitPlanMode result: the plan text serializes first.
        key: {"plan": mine.read_text(encoding="utf-8"), "isAgent": False, "filePath": str(mine)},
    }
    assert plan_doc.plan_path(payload) == str(mine)
    assert bookmark._extract_plan_info(payload) == (str(mine), "My plan")


def test_bookmark_never_takes_another_sessions_newest_plan(bookmark, home):
    _write_plan(home, "a.md", "# A\n")
    _write_plan(home, "b.md", "# B\n")
    assert bookmark._extract_plan_info({"tool_input": {}, "session_id": "nomatch"}) == ("", "")


def test_bookmark_names_the_session_that_exited_plan_mode(bookmark, monkeypatch):
    # Pinned: a dispatched session running this suite exports GENESIS_CC_SESSION=1,
    # which the hook deliberately skips (test below).
    monkeypatch.delenv("GENESIS_CC_SESSION", raising=False)
    assert bookmark._session_hint({"session_id": "mine-123"}) == "mine-123"


def test_background_sessions_are_not_bookmarked(bookmark, monkeypatch):
    monkeypatch.setenv("GENESIS_CC_SESSION", "1")
    assert bookmark._session_hint({"session_id": "mine-123"}) == ""


def _gate(payload: dict, home: Path, cwd: Path) -> subprocess.CompletedProcess:
    body = {"hook_event_name": "PreToolUse", "session_id": "test", "cwd": str(cwd), **payload}
    return subprocess.run(
        [sys.executable, str(_GATE), "--plan"],
        input=json.dumps(body),
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(home)},
        timeout=30,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    (r / "src" / "genesis").mkdir(parents=True)
    for args in (
        ["git", "init", "-q", "-b", "main"],
        ["git", "config", "user.email", "t@t"],
        ["git", "config", "user.name", "t"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(args, cwd=r, check=True, capture_output=True)
    return r


@pytest.mark.parametrize(
    "divider",
    [
        "# ═══ SUPERSEDED BELOW — resume block kept for history",
        "## ═══ SUPERSEDED BELOW ═══ (rev 1, kept for provenance)",
    ],
)
def test_the_gate_ignores_old_proposals_below_any_real_divider(home, repo, divider):
    """A new source file proposed only in archaeology must not block today's exit."""
    plan = _write_plan(
        home,
        "plan.md",
        f"# Plan\n\nFix a typo in docs.\n\n{divider}\n\nAdd src/genesis/brand_new.py\n",
    )
    result = _gate(_real_payload(plan), home, repo)
    assert result.returncode == 0, result.stderr


def test_the_gate_still_blocks_a_live_proposal_above_the_divider(home, repo):
    plan = _write_plan(
        home,
        "plan.md",
        "# Plan\n\nAdd src/genesis/brand_new.py\n\n# ═══ SUPERSEDED BELOW\n\nold\n",
    )
    result = _gate(_real_payload(plan), home, repo)
    assert result.returncode == 2 and "adopt-first" in result.stderr


def test_a_fenced_divider_example_does_not_hide_a_live_proposal(home, repo):
    plan = _write_plan(
        home,
        "plan.md",
        "# Plan\n\n```\n## ═══ SUPERSEDED BELOW ═══\n```\n\nAdd src/genesis/brand_new.py\n",
    )
    result = _gate(_real_payload(plan), home, repo)
    assert result.returncode == 2 and "adopt-first" in result.stderr


def test_a_nested_fenced_divider_does_not_hide_a_live_proposal(home, repo):
    plan = _write_plan(
        home,
        "plan.md",
        "# Plan\n\n````\n```\n## SUPERSEDED BELOW\n```\n````\n\nAdd src/genesis/brand_new.py\n",
    )
    result = _gate(_real_payload(plan), home, repo)
    assert result.returncode == 2 and "adopt-first" in result.stderr



_VERDICT = "## Adopt / Adapt / Build\n\nBUILD — no substitute. Searched: pypi, github\n"


def test_an_unpaired_nested_fence_does_not_hide_a_live_proposal(home, repo):
    """The gate's own fence regex closed a ```` block at the first bare ``` line."""
    plan = _write_plan(
        home, "plan.md", "# Plan\n\n````md\n```md\nfoo\n```\n````\n\nAdd src/genesis/brand_new.py\n"
    )
    result = _gate(_real_payload(plan), home, repo)
    assert result.returncode == 2 and "adopt-first" in result.stderr


def test_a_verdict_shown_only_inside_a_nested_example_is_not_a_verdict(home, repo):
    plan = _write_plan(
        home,
        "plan.md",
        f"# Plan\n\nAdd src/genesis/brand_new.py\n\n````\n```\n{_VERDICT}```\n````\n",
    )
    result = _gate(_real_payload(plan), home, repo)
    assert result.returncode == 2 and "adopt-first" in result.stderr


def test_an_unclosed_fence_never_lets_an_old_verdict_below_the_divider_clear_a_proposal(home, repo):
    plan = _write_plan(
        home,
        "plan.md",
        f"# Plan\n\nAdd src/genesis/brand_new.py\n\n```\nexample\n\n## SUPERSEDED BELOW\n\n{_VERDICT}",
    )
    result = _gate(_real_payload(plan), home, repo)
    assert result.returncode == 2 and "adopt-first" in result.stderr


@pytest.mark.parametrize(
    "text",
    [
        "# Plan\n\n<!--\n## SUPERSEDED BELOW\n-->\n\nstill live\n",
        "# SUPERSEDED BELOW: teach the hooks the divider\n\nstill live\n",
        "# Plan\n\nline\u2028## SUPERSEDED BELOW\n\nstill live\n",
    ],
)
def test_readings_that_would_empty_the_live_plan_do_not_divide(plan_doc, text):
    assert "still live" in plan_doc.live_section(text)


def test_an_unreadable_plan_path_falls_back_to_the_plan_text(home, repo):
    payload = {
        "tool_name": "ExitPlanMode",
        "tool_input": {
            "plan": "# Plan\n\nAdd src/genesis/brand_new.py\n",
            "planFilePath": str(home / ".claude" / "plans" / "gone.md"),
        },
    }
    result = _gate(payload, home, repo)
    assert result.returncode == 2 and "adopt-first" in result.stderr



# ── round 3: the plan is read as CommonMark renders it ──────────────────────
@pytest.mark.parametrize(
    "text",
    [
        # a closed example stays quoted when a later fence never closes (Codex r2)
        "# Plan\n\n```\n## SUPERSEDED BELOW\n```\n\nstill live\n\n```\nunclosed\n",
        # a comment opened after prose hides its heading (Codex r2)
        "# Plan\n\n<!--\nnote\n## SUPERSEDED BELOW\n-->\n\nstill live\n",
        # quoted or listed, the phrase is not a top-level heading
        "# Plan\n\n> ## SUPERSEDED BELOW\n\nstill live\n",
        "# Plan\n\n- item\n\n  ## SUPERSEDED BELOW\n\nstill live\n",
        # nothing above it: a plan titled with the phrase
        "---\nstatus: draft\n---\n# SUPERSEDED BELOW handling\n\nstill live\n",
    ],
)
def test_the_phrase_that_does_not_render_as_a_divider_keeps_the_plan_live(plan_doc, text):
    assert "still live" in plan_doc.live_section(text)


@pytest.mark.parametrize(
    "text",
    [
        # a title-less plan: prose, then the divider (Codex r2)
        "Intro prose, no title.\n\n## SUPERSEDED BELOW\n\nold\n",
        # an inline `<!--` in code does not open a comment (premise check)
        "# Plan\n\nWrite `<!--` to comment.\n\n## SUPERSEDED BELOW\n\nold\n",
        # a setext heading renders as a heading
        "# Plan\n\nlive\n\nSUPERSEDED BELOW\n----------------\n\nold\n",
        "---\nstatus: draft\n---\n# Plan\n\nlive\n\n## SUPERSEDED BELOW\n\nold\n",
    ],
)
def test_a_rendered_divider_divides(plan_doc, text):
    assert "old" not in plan_doc.live_section(text)


def test_without_a_parser_the_whole_plan_is_live_and_nothing_is_stripped(plan_doc, monkeypatch):
    monkeypatch.setattr(plan_doc, "_parser", lambda: None)
    text = "# Plan\n\n```\nsrc/genesis/x.py\n```\n\n## SUPERSEDED BELOW\n\nold\n"
    assert plan_doc.live_section(text) == text
    assert plan_doc.strip_quoted(text) == text


def test_html_comments_and_indented_code_are_quoted_material(plan_doc):
    text = "# Plan\n\n<!-- src/genesis/a.py -->\n\n    src/genesis/b.py\n\nAdd src/genesis/c.py\n"
    stripped = plan_doc.strip_quoted(text)
    assert "a.py" not in stripped and "b.py" not in stripped and "c.py" in stripped



# ── round 3 confirmation findings ───────────────────────────────────────────
@pytest.mark.parametrize(
    "text",
    [
        "# Plan\n\nlive\n\n## **SUPERSEDED BELOW**\n\nold\n",
        "# Plan\n\nlive\n\n## *═══ SUPERSEDED BELOW ═══*\n\nold\n",
    ],
)
def test_emphasis_does_not_hide_a_divider(plan_doc, text):
    assert "old" not in plan_doc.live_section(text)


@pytest.mark.parametrize(
    "text",
    [
        # a thematic break, a live plan, another break: not frontmatter
        "---\n# Plan\n\nstill live\n\n---\n\n## SUPERSEDED BELOW\n\nold\n",
        # an indented rule inside a block scalar does not close real frontmatter
        "---\nnote: |\n  ---\nstatus: x\n---\n# Plan\n\nstill live\n\n## SUPERSEDED BELOW\n\nold\n",
    ],
)
def test_only_real_frontmatter_is_skipped(plan_doc, text):
    live = plan_doc.live_section(text)
    assert "still live" in live and "old" not in live


def test_a_leading_comment_does_not_make_the_title_a_divider(plan_doc):
    text = "<!-- generated -->\n# SUPERSEDED BELOW handling\n\nstill live\n"
    assert "still live" in plan_doc.live_section(text)
