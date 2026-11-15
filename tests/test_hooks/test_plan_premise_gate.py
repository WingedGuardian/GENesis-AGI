"""The plan premise check: a plan naming repo work carries its premise answers.

Run as the harness runs it, a subprocess reading a real ExitPlanMode payload, with a
throwaway HOME, so no test touches the live install. The hook runs with its SHIPPED
``FORM_EPOCH``, so a plan owes the nine-item form (``plan_premise_gate.FORM``);
the six-question form is what ``test_premise_form.py`` pins.

What the design promises, and so what is tested:
* it blocks only a plan that names a repo path, and only for a missing section or a
  missing or empty answer, both of which are certain;
* it never judges whether an answer is true: an honest negative answer, or a tracker
  id this install does not hold, passes;
* it fails open and prints nothing on stdout, so it never volunteers a
  ``permissionDecision`` (an allow on ExitPlanMode would skip plan approval).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_HOOK = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "plan_premise_gate.py"

_LABELS = (
    "Observed",
    "Intended",
    "Cost",
    "Searched",
    "Acceptance",
    "Mechanism",
    "Alternatives",
    "Evidence",
    "Shape",
)

_L4 = "4. Searched: plan_doc.py and adopt_first_gate.py; no other reader."
_L6 = "6. Mechanism: the one reader is reused; its caller is adopt_first_gate, tracked by #42."
_L7 = "7. Alternatives: the shared reader already decides this; reused."
_L8 = "8. Evidence: 66 plans, counted by the replay script."
_L9 = "9. Shape: about 200 counted lines, one PR."

_COMPLETE = f"""# Fix the reader

Change `scripts/hooks/plan_doc.py` so it reads the right plan.

## Premise checks
1. Observed: the reader picks a stale plan (replay over 66 plans, main at abc123); not a stale tree.
2. Intended: the live plan is read. Stated: docs/x.md:40
3. Cost: every plan presented, counted by the replay script.
{_L4}
5. Acceptance: the live plan is read. Wrong fix: reading the newest file.
{_L6}
{_L7}
{_L8}
{_L9}
"""


def _ans(n: int, text: str | None = None, delim: str = ".") -> str:
    """Answer ``n`` with its label (and, for 2 and 5, its sub-label answered)."""
    text = text or f"answer {n}"
    sub = {2: " Stated: s", 5: " Wrong fix: w"}.get(n, "")
    return f"{n}{delim} {_LABELS[n - 1]}: {text}.{sub}\n"


def _answers(numbers, delim: str = ".") -> str:
    return "".join(_ans(n, delim=delim) for n in numbers)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    (h / ".claude" / "plans").mkdir(parents=True)
    return h


def _call(home: Path, payload: dict | str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GENESIS_")}
    env.pop("_TEST_PREMISE_FORM_EPOCH", None)  # the shipped epoch: plans owe the nine
    env["HOME"] = str(home)
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload if isinstance(payload, str) else json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _run(home: Path, plan: str) -> subprocess.CompletedProcess:
    path = home / ".claude" / "plans" / "plan.md"
    path.write_text(plan, encoding="utf-8")
    return _call(
        home,
        {
            "session_id": "s1",
            "cwd": str(home),
            "hook_event_name": "PreToolUse",
            "tool_name": "ExitPlanMode",
            "tool_input": {"plan": plan, "planFilePath": str(path)},
        },
    )


def test_a_complete_plan_passes_silently(home: Path) -> None:
    r = _run(home, _COMPLETE)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""


def test_a_plan_naming_no_repo_path_is_out_of_scope(home: Path) -> None:
    r = _run(home, "# Draft the newsletter\n\nResearch three sources and write it up.\n")
    assert r.returncode == 0, r.stderr


def test_a_home_or_absolute_path_is_not_repo_work(home: Path) -> None:
    r = _run(home, "# Tidy notes\n\nEdit ~/.claude/plans/old.md and /home/x/docs/a.md.\n")
    assert r.returncode == 0, r.stderr


def test_a_repo_path_in_a_code_block_is_not_repo_work(home: Path) -> None:
    r = _run(home, "# Research\n\nAn example:\n\n```\nscripts/hooks/x.py\n```\n")
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    "path",
    [
        "src/genesis/x.py",
        "scripts/x.sh",
        "tests/test_x.py",
        ".claude/skills/a/SKILL.md",
        "docs/a.md",
        "config/a.yaml",
        ".github/workflows/a.yml",
        "./src/genesis/x.py",
    ],
)
def test_each_repo_root_triggers(home: Path, path: str) -> None:
    r = _run(home, f"# Fix\n\nEdit `{path}`.\n")
    assert r.returncode == 2, path
    assert "no `## Premise checks` section" in r.stderr


def test_the_block_names_all_nine_items(home: Path) -> None:
    r = _run(home, "# Fix\n\nEdit src/genesis/memory/store.py.\n")
    assert r.returncode == 2
    for n, label in enumerate(_LABELS, start=1):
        assert f"  {n}. {label}: <" in r.stderr


def test_premise_questions_heading_is_accepted(home: Path) -> None:
    assert (
        _run(home, _COMPLETE.replace("## Premise checks", "## Premise questions")).returncode == 0
    )


@pytest.mark.parametrize("n", range(1, 10))
def test_each_missing_answer_is_named(home: Path, n: int) -> None:
    plan = "\n".join(line for line in _COMPLETE.split("\n") if not line.startswith(f"{n}. "))
    r = _run(home, plan)
    assert r.returncode == 2
    if n == 1:  # the form is told from answer 1, so without it the note says so
        assert "its first answer must start `Observed:`" in r.stderr
    else:
        assert f"answer {n} (" in r.stderr
        assert r.stderr.count(" is missing or empty") == 1


@pytest.mark.parametrize("empty", ["", "_No response_", "n/a", "N/A", "none", "—", "-", "...", "…"])
def test_an_empty_answer_is_blocked(home: Path, empty: str) -> None:
    plan = _COMPLETE.replace(_L8, f"8. Evidence: {empty}")
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 8 (" in r.stderr


def test_na_with_a_reason_is_an_answer(home: Path) -> None:
    plan = _COMPLETE.replace(_L8, "8. Evidence: n/a — the plan states no numbers.")
    assert _run(home, plan).returncode == 0


def test_an_honest_negative_answer_passes(home: Path) -> None:
    plan = _COMPLETE.replace(
        "plan_doc.py and adopt_first_gate.py; no other reader",
        "searched for scripts/hooks/premise_reader.py: it does not exist",
    )
    assert _run(home, plan).returncode == 0


def test_a_tracker_this_install_does_not_hold_passes(home: Path) -> None:
    plan = _COMPLETE.replace(
        "tracked by #42",
        "tracked by the other install's ledger row aaaaaaaabbbbbbbbccccccccdddddddd",
    )
    assert _run(home, plan).returncode == 0


def test_answers_accept_paren_numbering_and_continuation_lines(home: Path) -> None:
    plan = _COMPLETE.replace("9. Shape:", "9)\n   Shape:")
    for n in range(1, 9):
        plan = plan.replace(f"\n{n}. ", f"\n{n}) ")
    assert _run(home, plan).returncode == 0


def test_a_nested_numbered_list_does_not_overwrite_an_answer(home: Path) -> None:
    plan = _COMPLETE.replace(_L7, "7. Alternatives:\n    1. reader reused\n    2. nothing copied")
    assert _run(home, plan).returncode == 0


def test_a_nested_list_cannot_supply_a_missing_answer(home: Path) -> None:
    plan = _COMPLETE.replace(
        f"{_L7}\n{_L8}", "7. Alternatives: reused.\n    8. a nested item, not answer 8"
    )
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 8 (" in r.stderr


_HEAD = "# Fix\n\nEdit `scripts/x.py`.\n\n## Premise checks\n"


def test_the_section_ends_at_the_next_heading(home: Path) -> None:
    plan = _HEAD + _answers(range(1, 4)) + "\n## Steps\n" + _answers(range(4, 10))
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 4 (" in r.stderr


def test_a_premise_section_shown_in_a_code_block_does_not_count(home: Path) -> None:
    body = _COMPLETE.split("## Premise checks")[1]
    plan = "# Fix\n\nEdit `scripts/x.py`.\n\n```\n## Premise checks" + body + "```\n"
    r = _run(home, plan)
    assert r.returncode == 2
    assert "no `## Premise checks` section" in r.stderr


def test_the_superseded_part_does_not_count(home: Path) -> None:
    plan = "# Research only\n\nRead three papers.\n\n## ═══ SUPERSEDED BELOW ═══\n\nOld plan: edit `scripts/hooks/x.py`.\n"
    assert _run(home, plan).returncode == 0


def test_the_plan_text_is_read_when_the_file_is_gone(home: Path) -> None:
    r = _call(
        home,
        {
            "session_id": "s1",
            "tool_name": "ExitPlanMode",
            "tool_input": {
                "plan": "# Fix\n\nEdit `src/genesis/x.py`.\n",
                "planFilePath": str(home / "nowhere.md"),
            },
        },
    )
    assert r.returncode == 2


def test_a_block_is_not_softened_on_retry(home: Path) -> None:
    plan = "# Fix\n\nEdit `src/genesis/x.py`.\n"
    assert [_run(home, plan).returncode for _ in range(3)] == [2, 2, 2]


@pytest.mark.parametrize("stdin", ["", "not json", "[]", '{"tool_input": 5}'])
def test_a_bad_payload_fails_open_and_silent(home: Path, stdin: str) -> None:
    r = _call(home, stdin)
    assert r.returncode == 0
    assert r.stdout == ""


def test_a_later_list_in_the_section_cannot_blank_an_answer(home: Path) -> None:
    plan = _COMPLETE + "\nOpen items:\n1. _No response_\n"
    assert _run(home, plan).returncode == 0


def test_an_internal_error_fails_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import private_module

    gate = private_module("plan_premise_gate_crash", _HOOK)

    def boom(_payload: dict) -> int:
        raise RuntimeError("simulated")

    monkeypatch.setattr(gate, "read_payload", lambda: {"tool_name": "ExitPlanMode"})
    monkeypatch.setattr(gate, "_check", boom)
    assert gate.main() == 0


def test_an_undecodable_plan_file_falls_back_to_the_payload_text(home: Path) -> None:
    path = home / ".claude" / "plans" / "plan.md"
    path.write_bytes(b"# Fix\n\xff\xfe\n")
    r = _call(
        home,
        {
            "session_id": "s1",
            "tool_name": "ExitPlanMode",
            "tool_input": {
                "planFilePath": str(path),
                "plan": "# Fix\n\nEdit `src/genesis/x.py`.\n",
            },
        },
    )
    assert r.returncode == 2


def test_the_payload_text_outranks_a_guessed_plan_file(home: Path) -> None:
    (home / ".claude" / "plans" / "only.md").write_text("# Other\n\nEdit `src/genesis/x.py`.\n")
    r = _call(
        home,
        {"session_id": "s1", "tool_name": "ExitPlanMode", "tool_input": {"plan": _COMPLETE}},
    )
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    "heading",
    [
        "## Premise check",
        "# Premise checks",
        "#### Premise questions",
        "## **Premise checks**",
        "## Premise questions (plan-time)",
        "Premise checks\n--------------",
    ],
)
def test_real_heading_spellings_are_accepted(home: Path, heading: str) -> None:
    assert _run(home, _COMPLETE.replace("## Premise checks", heading)).returncode == 0


def test_an_autonumbered_list_counts_by_position(home: Path) -> None:
    plan = _COMPLETE
    for n in range(2, 10):
        plan = plan.replace(f"\n{n}. ", "\n1. ")
    assert _run(home, plan).returncode == 0


def test_a_skipped_number_names_the_missing_answer(home: Path) -> None:
    lines = [ln for ln in _COMPLETE.split("\n") if not ln.startswith("3. ")]
    r = _run(home, "\n".join(lines))
    assert r.returncode == 2
    assert "answer 3 (" in r.stderr
    assert r.stderr.count(" is missing or empty") == 1


def test_a_three_space_nested_item_cannot_supply_a_missing_answer(home: Path) -> None:
    plan = _COMPLETE.replace(
        f"{_L7}\n{_L8}",
        # The blank line makes the nesting real: a list numbered 8 cannot interrupt
        # a paragraph, so without it CommonMark reads "8." as text.
        "7. Alternatives: reused.\n\n   8. a nested item, not answer 8",
    )
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 8 (" in r.stderr


def test_text_after_the_list_does_not_fill_an_empty_answer(home: Path) -> None:
    plan = _COMPLETE.replace(_L9, "9.\n\nShape is about 200 lines.")
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 9 (" in r.stderr


def test_answers_run_together_on_one_line_are_one_answer(home: Path) -> None:
    run_together = "1. Observed: a. 2. Intended: b. Stated: c. 3. Cost: d.\n"
    plan = _HEAD + run_together + _answers(range(4, 10))
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 2 (" in r.stderr


@pytest.mark.parametrize("empty", ["TBD", "todo", "?", "n/a.", "n/a —", "<the caller>", "None."])
def test_placeholders_are_empty(home: Path, empty: str) -> None:
    plan = _COMPLETE.replace(_L6, f"6. Mechanism: {empty}")
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 6 (" in r.stderr


def test_a_fifo_plan_path_falls_back_to_the_payload_text(home: Path) -> None:
    fifo = home / "plan.fifo"
    os.mkfifo(fifo)
    r = _call(
        home,
        {
            "session_id": "s1",
            "tool_name": "ExitPlanMode",
            "tool_input": {"planFilePath": str(fifo), "plan": "# Fix\n\nEdit `src/genesis/x.py`.\n"},
        },
    )
    assert r.returncode == 2  # read the payload's plan, did not hang on the FIFO


def test_a_device_plan_path_is_not_read(home: Path) -> None:
    r = _call(
        home,
        {
            "session_id": "s1",
            "tool_name": "ExitPlanMode",
            "tool_input": {"planFilePath": "/dev/zero", "plan": _COMPLETE},
        },
    )
    assert r.returncode == 0, r.stderr


def test_a_trailing_list_cannot_fill_a_missing_answer(home: Path) -> None:
    plan = _HEAD + _answers([1, 2, 4, 5, 6, 7, 8, 9]) + "\nOpen items:\n1. still to do\n"
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 3 (" in r.stderr


def test_a_preamble_list_cannot_shift_empty_answers_away(home: Path) -> None:
    # Parser mechanics (owner ruling: these may be pinned outside the form): an
    # earlier numbered list cannot hide a later list's empty answers.
    from tests.conftest import private_module

    doc = private_module("plan_doc_preamble", _HOOK.parent / "plan_doc.py")
    section = "## Premise checks\nChecked:\n1. x\n2. y\n\nAnswers:\n1. a\n2. b\n3. c\n4. d\n5.\n6.\n"
    answers = doc.premise_answers(section)
    assert answers[5] == "" and answers[6] == ""
    # Through the hook, the preamble takes answers 1 and 2, so the note fires.
    plan = _HEAD + "Checked:\n1. x\n2. y\n\nAnswers:\n" + _answers(range(1, 8)) + "8.\n9.\n"
    r = _run(home, plan)
    assert r.returncode == 2 and "its first answer must start `Observed:`" in r.stderr


@pytest.mark.parametrize("decorated", ["**N/A**", "`n/a`", "_TBD_", "*?*", "[](https://example.com)"])
def test_an_answer_that_renders_empty_is_empty(home: Path, decorated: str) -> None:
    plan = _COMPLETE.replace(_L9, f"9. Shape: {decorated}")
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 9 (" in r.stderr


def test_a_link_with_text_is_an_answer(home: Path) -> None:
    plan = _COMPLETE.replace(_L9, "9. Shape: [the shape note](https://example.com)")
    assert _run(home, plan).returncode == 0


def test_the_remedy_template_copied_verbatim_is_nine_empty_answers(home: Path) -> None:
    first = _run(home, "# Fix\n\nEdit `src/genesis/x.py`.\n")
    template = first.stderr.split("## Premise checks\n", 1)[1].split("\n\n", 1)[0]
    plan = _HEAD + "\n".join(line.strip() for line in template.splitlines()) + "\n"
    r = _run(home, plan)
    assert r.returncode == 2
    assert sum(line.startswith("  - answer ") for line in r.stderr.splitlines()) == 9


def test_a_block_writes_nothing_on_stdout(home: Path) -> None:
    r = _run(home, "# Fix\n\nEdit `src/genesis/x.py`.\n")
    assert r.returncode == 2
    assert r.stdout == ""


def test_a_longer_list_after_the_next_heading_is_not_the_answers(home: Path) -> None:
    steps = "".join(f"{n}. step {n}\n" for n in range(1, 12))
    plan = _HEAD + _answers(range(1, 4)) + "\n## Steps\n" + steps
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 4 (" in r.stderr


def test_a_short_preamble_list_ahead_of_the_answers_blocks_with_the_note(home: Path) -> None:
    """The first list carrying a number supplies that answer (plan_doc), so a
    numbered preamble supplies answers 1 and 2 and the section no longer reads as
    the nine-item form. It fails closed, with the note naming answer 1. A residual,
    MEASURED rare: 0 of 74 premise sections in one install's plans (2026-11-15)
    put a numbered list ahead of their answers; the one with two lists put the
    answers first."""
    body = _COMPLETE.split("## Premise checks\n", 1)[1]
    plan = _HEAD + "Checked first:\n1. the reader\n2. the gate\n\nAnswers:\n" + body
    r = _run(home, plan)
    assert r.returncode == 2 and "its first answer must start `Observed:`" in r.stderr
    bullets = _HEAD + "Checked first:\n- the reader\n- the gate\n\nAnswers:\n" + body
    assert _run(home, bullets).returncode == 0  # the one-edit fix: a bulleted preamble


def test_answer_text_joins_continuation_lines_with_a_space() -> None:
    from tests.conftest import private_module

    doc = private_module("plan_doc_text", _HOOK.parent / "plan_doc.py")
    answers = doc.premise_answers("## Premise checks\n1. Shape:\n   about 200 lines\n")
    assert answers == {1: "Shape: about 200 lines"}


_FULL = _answers(range(1, 10))


@pytest.mark.parametrize(
    "split",
    ["\nEvidence: x\n\n", "\n---\n\n", "\n> a quoted note\n\n", ")"],
    ids=["paragraph", "rule", "blockquote", "delimiter-change"],
)
def test_answers_split_into_several_lists_all_count(home: Path, split: str) -> None:
    if split == ")":
        body = _answers(range(1, 4)) + _answers(range(4, 10), delim=")")
    else:
        body = _answers(range(1, 4)) + split + _answers(range(4, 10))
    assert _run(home, _HEAD + body).returncode == 0


def test_a_longer_steps_list_in_the_section_cannot_replace_empty_answers(home: Path) -> None:
    steps = "".join(f"{n}. step {n}\n" for n in range(1, 12))
    plan = _HEAD + _FULL.replace(_ans(9), "9.\n") + "\nSteps:\n" + steps
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 9 (" in r.stderr


def test_a_long_list_under_a_subheading_cannot_replace_empty_answers(home: Path) -> None:
    evidence = "".join(f"{n}. evidence {n}\n" for n in range(1, 11))
    plan = _HEAD + _FULL.replace(_ans(9), "9. Shape: TBD\n") + "\n### Evidence\n" + evidence
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 9 (" in r.stderr


def test_an_answer_written_inside_the_brackets_is_named_as_a_placeholder(home: Path) -> None:
    plan = _COMPLETE.replace(_L6, "6. Mechanism: <caller: adopt_first_gate, tracked by #42>")
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 6 (" in r.stderr and "only a `<placeholder>`" in r.stderr


@pytest.mark.parametrize("empty", ["none —", "TBD:", "N.A.", "not applicable", "– –", "​"])
def test_more_placeholder_spellings_are_empty(home: Path, empty: str) -> None:
    plan = _COMPLETE.replace(_L8, f"8. Evidence: {empty}")
    r = _run(home, plan)
    assert r.returncode == 2
    assert "answer 8 (" in r.stderr
