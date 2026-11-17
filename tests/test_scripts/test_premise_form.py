"""The nine-item premise form, read alongside the six-question form.

The new form labels each answer (``1. Observed: ...`` through ``9. Shape: ...``).
Items 1 to 5 are the Problem block, owed by every issue; 6 to 9 the Change
block, owed by plans and by an issue handed to a paid builder (``devin-ready``).
Answer 2 carries ``Stated:`` and answer 5 ``Wrong fix:``. ``1. Problem: as #N``
stands for items 1 to 5.

``FORM_EPOCH`` is set, and the tests run at the shipped value unless they set
their own. Every plan and paid build owes the nine-item form, whatever the date.
An issue created on or after the epoch owes it; one created before it keeps the
either-form rule, under which a complete section in EITHER form passes and an
incomplete one is reported in the form it was written in. Most issue fixtures
here are dated before the epoch, so they exercise that rule.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module

_ROOT = Path(__file__).resolve().parents[2]
gate = private_module("premise_gate_form_under_test", _ROOT / "scripts" / "premise_gate.py")
_PLAN_HOOK = _ROOT / "scripts" / "hooks" / "plan_premise_gate.py"

_NEW = "2026-11-20T00:00:00Z"

_OLD = "2026-10-01T00:00:00Z"


@pytest.fixture(autouse=True)
def _shipped_epoch(monkeypatch):
    """Every test runs at the SHIPPED epoch unless it sets its own; no
    override leaks in from the environment. The default issue date falls before
    it, so issues here take the either-form rule unless a test says otherwise."""
    monkeypatch.delenv("_TEST_PREMISE_FORM_EPOCH", raising=False)
    assert _NEW < gate._plan_gate.FORM_EPOCH

_PROBLEM = """1. Observed: the guard reports a migration as deleted (`pytest tests/x.py`, main at abc123); not a stale tree.
2. Intended: a migration merged after checkout is not reported. Stated: docs/x.md:40
3. Cost: every PR opened before a migration lands, about 3 a week, counted from CI runs.
4. Searched: issues for "migration guard", main, rulings; nothing.
5. Acceptance: a migration merged after checkout passes, a deleted one is still reported. Wrong fix: pinning the base to HEAD.
"""
_CHANGE = """6. Mechanism: compare against the merge base; the CI step passes it.
7. Alternatives: the existing base helper decides this; reused.
8. Evidence: the measured CI runs above.
9. Shape: about 40 counted lines, one PR.
"""
_OLD_SIX = """1. n/a — no outside behaviour.
2. Searched scripts/ for x; nothing.
3. Scope reuses the shared reader.
4. Caller: the CI job, tracked by #42.
5. No numbers.
6. About 100 counted lines, one PR.
"""


def _issue(answers: str) -> str:
    return f"## Summary\nDo the thing in `scripts/x.py`.\n\n## Premise checks\n{answers}"


def _event(action, body, *, labels=(), label=None, created=_NEW):
    ev = {
        "action": action,
        "issue": {
            "number": 7,
            "body": body,
            "created_at": created,
            "labels": [{"name": n} for n in labels],
        },
    }
    if label:
        ev["label"] = {"name": label}
    return ev


def _opened(body, created=_NEW):
    return gate.decide(_event("opened", body, created=created))


def _devin(body, created=_NEW):
    return gate.decide(
        _event("labeled", body, labels=["devin-ready"], label="devin-ready", created=created)
    )


# -- issues created before the epoch (the either-form rule) -----------------------


def test_a_new_form_problem_block_alone_completes_an_issue():
    assert _opened(_issue(_PROBLEM)).add == []


def test_a_new_form_issue_without_wrong_fix_is_incomplete():
    body = _issue(_PROBLEM.replace(" Wrong fix: pinning the base to HEAD.", ""))
    assert _opened(body).add == ["premises-missing"]


def test_a_new_form_issue_without_stated_is_incomplete():
    body = _issue(_PROBLEM.replace(" Stated: docs/x.md:40", ""))
    assert _opened(body).add == ["premises-missing"]


def test_a_label_with_nothing_after_it_is_empty():
    lines = _PROBLEM.splitlines()
    lines[2] = "3. Cost:"
    assert _opened(_issue("\n".join(lines) + "\n")).add == ["premises-missing"]
    lines[2] = "3. Cost: n/a"  # a bare n/a after the label is empty too
    assert _opened(_issue("\n".join(lines) + "\n")).add == ["premises-missing"]


def test_an_answer_without_its_label_is_incomplete():
    body = _issue(_PROBLEM.replace("3. Cost: every PR", "3. Every PR"))
    assert _opened(body).add == ["premises-missing"]


def test_a_change_block_on_an_issue_is_complete_if_present(epoch):
    """Once the form is required. Before it, six non-empty answers pass in either
    form, as they do today."""
    partial = _issue(_PROBLEM + "6. Mechanism: compare against the merge base.\n")
    assert _opened(partial).add == ["premises-missing"]
    assert _opened(_issue(_PROBLEM + _CHANGE)).add == []


def test_the_old_six_still_complete_an_issue_created_before_the_epoch():
    assert _opened(_issue(_OLD_SIX)).add == []


# -- devin-ready (owes the nine whatever the date) --------------------------------


def test_devin_ready_on_a_complete_nine_stays():
    plan = _devin(_issue(_PROBLEM + _CHANGE))
    assert not plan.remove and not plan.comment


def test_devin_ready_on_a_problem_block_alone_is_removed_naming_the_change_block():
    plan = _devin(_issue(_PROBLEM))
    assert plan.remove == ["devin-ready"]
    assert "4 of 9" in plan.comment
    assert "6. Mechanism" in plan.comment and "9. Shape" in plan.comment
    assert "1. Observed" not in plan.comment




# -- issues and devin-ready, FORM_EPOCH set --------------------------------------


@pytest.fixture
def epoch(monkeypatch):
    monkeypatch.setattr(gate._plan_gate, "FORM_EPOCH", "2026-11-15T00:00:00Z")


def test_after_the_epoch_a_new_issue_owes_the_new_form(epoch):
    assert _opened(_issue(_OLD_SIX)).add == ["premises-missing"]
    assert _opened(_issue(_PROBLEM)).add == []


def test_after_the_epoch_an_older_issue_keeps_the_old_rule(epoch):
    assert _opened(_issue(_OLD_SIX), created="2026-11-01T00:00:00Z").add == []


def test_after_the_epoch_devin_ready_owes_the_nine(epoch):
    plan = _devin(_issue(_OLD_SIX), created=_OLD)
    assert plan.remove == ["devin-ready"] and "of 9" in plan.comment


# -- plans (the ExitPlanMode hook, run as the harness runs it) -------------------


def _plan(answers: str | None) -> str:
    body = "# Fix the reader\n\nChange `scripts/hooks/plan_doc.py`.\n"
    return body if answers is None else body + "\n## Premise checks\n" + answers


def _run_plan(tmp_path: Path, plan: str, *, epoch: str | None = None):
    home = tmp_path / "home"
    (home / ".claude" / "plans").mkdir(parents=True, exist_ok=True)
    path = home / ".claude" / "plans" / "plan.md"
    path.write_text(plan, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GENESIS_")}
    env["HOME"] = str(home)
    if epoch:
        env["_TEST_PREMISE_FORM_EPOCH"] = epoch
    payload = {
        "session_id": "s1",
        "cwd": str(home),
        "hook_event_name": "PreToolUse",
        "tool_name": "ExitPlanMode",
        "tool_input": {"plan": plan, "planFilePath": str(path)},
    }
    return subprocess.run(
        [sys.executable, str(_PLAN_HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def test_a_plan_in_the_new_form_passes(tmp_path):
    r = _run_plan(tmp_path, _plan(_PROBLEM + _CHANGE))
    assert r.returncode == 0, r.stderr


def test_a_plan_citing_its_issue_for_the_problem_block_passes(tmp_path):
    r = _run_plan(tmp_path, _plan("1. Problem: as #123\n" + _CHANGE))
    assert r.returncode == 0, r.stderr


def test_a_new_form_plan_without_alternatives_is_blocked_naming_it(tmp_path):
    answers = "1. Problem: as #123\n" + _CHANGE.replace(
        "7. Alternatives: the existing base helper decides this; reused.\n", ""
    )
    r = _run_plan(tmp_path, _plan(answers))
    assert r.returncode == 2
    assert "7 (Alternatives" in r.stderr


def test_a_plan_with_no_section_gets_the_nine_item_template(tmp_path):
    r = _run_plan(tmp_path, _plan(None))
    assert r.returncode == 2
    assert "1. Observed: <" in r.stderr and "9. Shape: <" in r.stderr
    assert "outside-behaviour claims measured or cited" not in r.stderr



# -- the messages a writer sees ---------------------------------------------------


def test_the_owed_message_for_a_new_form_issue_names_the_label_and_the_reason():
    body = _issue(_PROBLEM.replace(" Wrong fix: pinning the base to HEAD.", ""))
    msg = gate.owed_message({"body": body, "labels": [], "created_at": _NEW})
    assert "answer 5 (Acceptance) has no `Wrong fix:` answer" in msg
    # The label sits outside the placeholder, so a writer filling the template in
    # keeps it; a template of `5. <Acceptance: ...>` would lose it.
    assert "5. Acceptance: <what must hold afterwards" in msg
    assert "<Acceptance" not in msg


def test_check_reports_the_change_block_a_paid_build_owes(capsys):
    body = _issue(_PROBLEM)
    code = gate.check(7, view=lambda n: {"body": body, "labels": []})
    out = capsys.readouterr().out
    assert code == 1
    assert "missing premise answer 6: Mechanism:" in out and "(4 of 9 missing)" in out
    assert gate.check(7, view=lambda n: {"body": _issue(_PROBLEM + _CHANGE), "labels": []}) == 0


# -- review round (pre-push): an old six whose answers happen to look new --------

_OLD_SIX_OBSERVED = _OLD_SIX.replace(
    "1. n/a — no outside behaviour.", "1. Observed: gh's compare semantics, cited from its docs."
)
_OLD_SIX_AS = _OLD_SIX.replace("1. n/a — no outside behaviour.", "1. Problem: as #12 explains it.")


@pytest.mark.parametrize("body", [_OLD_SIX_OBSERVED, _OLD_SIX_AS])
def test_a_complete_six_that_starts_like_the_new_form_still_passes_for_an_older_issue(
    body, tmp_path
):
    """Old question 1 is naturally answered 'Observed: ...'; an issue created
    before the epoch keeps the either-form rule, so a complete six passes the
    label job. A paid build and a plan owe the nine whatever the date."""
    assert _opened(_issue(body)).add == []
    plan = _devin(_issue(body), created=_OLD)
    assert plan.remove == ["devin-ready"]
    assert _run_plan(tmp_path, _plan(body)).returncode == 2


def test_a_template_left_in_the_change_block_is_not_a_written_change_block():
    filled = _issue(
        _PROBLEM
        + "6. Mechanism: <why this removes the cause>\n7. Alternatives:\n"
        + "8. Evidence: <every claim>\n9. Shape: <counted lines>\n"
    )
    assert _opened(filled).add == []


def test_the_as_line_is_for_plans_only():
    """An issue cites no other issue for its own Problem block, and a paid build
    owes all nine written out (spec 5.2)."""
    assert _opened(_issue("1. Problem: as #4602\n" + _CHANGE)).add == ["premises-missing"]
    plan = _devin(_issue("1. Problem: as #4602\n" + _CHANGE))
    assert plan.remove == ["devin-ready"]


def test_the_devin_comment_names_why_a_present_answer_does_not_count(epoch):
    body = _issue(_PROBLEM.replace(" Stated: docs/x.md:40", "") + _CHANGE)
    plan = _devin(body)
    assert "has no `Stated:` answer" in plan.comment


def test_wrong_fix_tolerates_spacing():
    body = _issue(_PROBLEM.replace("Wrong fix:", "Wrong  fix :"))
    assert _opened(body).add == []


def test_an_empty_epoch_override_falls_back_to_the_constant(monkeypatch):
    monkeypatch.setenv("_TEST_PREMISE_FORM_EPOCH", "")
    assert gate._plan_gate.form_epoch() == _SHIPPED  # the constant
    monkeypatch.setattr(gate._plan_gate, "FORM_EPOCH", "2026-11-15T00:00:00Z")
    assert gate._plan_gate.form_epoch() == "2026-11-15T00:00:00Z"


_TWO_COINCIDENCES = _OLD_SIX.replace(
    "1. n/a — no outside behaviour.", "1. Observed: gh's compare semantics, cited."
).replace("4. Caller: the CI job, tracked by #42.", "4. Searched: callers; the CI job, #42.")


def test_an_old_six_with_two_label_coincidences_still_passes_for_an_older_issue(tmp_path):
    """The re-review's construction: answer 1 opens 'Observed:' and answer 4
    'Searched:', which the detector reads as the nine-item form. An issue created
    before the epoch still passes; a paid build and a plan owe the nine."""
    assert _opened(_issue(_TWO_COINCIDENCES)).add == []
    plan = _devin(_issue(_TWO_COINCIDENCES), created=_OLD)
    assert plan.remove == ["devin-ready"]
    assert _run_plan(tmp_path, _plan(_TWO_COINCIDENCES)).returncode == 2


def test_six_non_empty_answers_pass_in_either_form_before_the_epoch():
    """A nine-item section missing a sub-label still passes before the epoch:
    main passes any six non-empty answers today."""
    body = _issue(_PROBLEM.replace(" Stated: docs/x.md:40", "") + _CHANGE)
    assert _opened(body).add == []


def test_after_the_epoch_an_older_issue_keeps_the_old_rule_even_with_coincidences(epoch):
    """Grandfathering survives the epoch for issues created before it: the
    either-form rule still applies to them (secondary review, P3)."""
    assert _opened(_issue(_TWO_COINCIDENCES), created="2026-11-01T00:00:00Z").add == []


# -- round 1: an answer is judged on its own field, with labels taken off ---------

_TEMPLATE_ISSUE = gate._plan_gate._form_template()
_BARE_LABELS = "".join(f"{n}. {label}:\n" for n, (label, _) in enumerate(gate._plan_gate.FORM, start=1))


def _gaps(body, *, build=False, created=_NEW):
    return gate.premise_gaps(body, build=build, created_at=created)[1]


@pytest.mark.parametrize(
    "old, new, n",
    [
        # M1, M2: a filled sub-label never fills an empty main answer.
        ("2. Intended: a migration merged after checkout is not reported. Stated: docs/x.md:40",
         "2. Intended:  Stated: docs/x.md:40", 2),
        ("5. Acceptance: a migration merged after checkout passes, a deleted one is still reported. "
         "Wrong fix: pinning the base to HEAD.", "5. Acceptance:  Wrong fix: none, because no fix is proposed", 5),
        # M3: a word in prose is not the sub-label.
        ("2. Intended: a migration merged after checkout is not reported. Stated: docs/x.md:40",
         "2. Intended: works as stated: nothing written", 2),
        # M4: a main answer that is only another item's label.
        ("3. Cost: every PR opened before a migration lands, about 3 a week, counted from CI runs.",
         "3. Cost: Searched:", 3),
        # M5: a placeholder main answer with the sub-label filled.
        ("2. Intended: a migration merged after checkout is not reported. Stated: docs/x.md:40",
         "2. Intended: <what should happen> Stated: docs/x.md", 2),
    ],
)
def test_each_field_is_judged_on_its_own(old, new, n):
    body = _issue(_PROBLEM.replace(old, new))
    assert n in [k for k, _ in gate._plan_gate.form_problems(
        gate.premise_answers(gate.strip_quoted(body)), gate._plan_gate.PROBLEM_BLOCK)]


def test_a_sub_label_after_a_sentence_end_or_capitalized_counts():
    for answer in ("2. Intended: it passes. stated: docs/x.md:40", "2. Intended: it passes Stated: docs/x.md:40"):
        body = _issue(_PROBLEM.replace(
            "2. Intended: a migration merged after checkout is not reported. Stated: docs/x.md:40", answer))
        found = gate._plan_gate.form_problems(gate.premise_answers(gate.strip_quoted(body)), (2,))
        assert found == [], (answer, found)


@pytest.mark.parametrize("section", [_TEMPLATE_ISSUE, _BARE_LABELS])
def test_an_unfilled_nine_item_section_is_not_six_answers(section):
    # M6: the issue label, the devin-ready guard and check, before the epoch.
    body = _issue(section)
    assert _opened(body).add == ["premises-missing"]
    assert _gaps(body) and _gaps(body, build=True)
    plan = _devin(body, created=_OLD)
    assert "devin-ready" in plan.remove


@pytest.mark.parametrize("section", [_TEMPLATE_ISSUE, _BARE_LABELS])
def test_the_plan_gate_blocks_an_unfilled_nine_item_section(tmp_path, section):
    # M7.
    assert _run_plan(tmp_path, _plan(section)).returncode == 2


def test_a_grandfathered_unfilled_template_still_owes_its_answers(epoch):
    # M8: an issue created before the epoch keeps the either-form rule, but bare
    # labels are not six answers under it either.
    assert _opened(_issue(_TEMPLATE_ISSUE), created="2026-11-01T00:00:00Z").add == ["premises-missing"]


def test_a_complete_new_form_and_an_old_six_still_pass_on_an_older_issue(tmp_path):
    # An old six the detector claims (answer 1 opens 'Observed:', answer 4
    # 'Searched:'), with answers that open with a form word and say little:
    # today's rule passes it, so it still passes.
    claimed = _OLD_SIX.replace("1. n/a — no outside behaviour.", "1. Observed: no outside behaviour.").replace(
        "2. Searched scripts/ for x; nothing.", "2. Shape: none").replace(
        "4. Caller: the CI job, tracked by #42.", "4. Searched: caller is #12")
    assert gate._plan_gate.is_new_form(gate.premise_answers(gate.strip_quoted(_issue(claimed))))
    assert _opened(_issue(claimed)).add == []
    assert _run_plan(tmp_path, _plan(claimed)).returncode == 2  # a plan owes the nine
    assert _opened(_issue(_PROBLEM)).add == []
    assert _opened(_issue(_PROBLEM + _CHANGE)).add == []
    assert _opened(_issue(_OLD_SIX)).add == []


def test_items_left_as_the_template_placeholder_are_unanswered(tmp_path, epoch):
    """The template's own placeholder names the sub-label; it is not the answer."""
    template = {n: line for n, line in enumerate(gate._plan_gate._form_template().splitlines(), start=1)}
    body = _PROBLEM
    body = body.replace(
        "2. Intended: a migration merged after checkout is not reported. Stated: docs/x.md:40",
        template[2].strip())
    body = body.replace(
        "5. Acceptance: a migration merged after checkout passes, a deleted one is still reported. "
        "Wrong fix: pinning the base to HEAD.", template[5].strip())
    answers = gate.premise_answers(gate.strip_quoted(_issue(body)))
    assert [n for n, _ in gate._plan_gate.form_problems(answers, gate._plan_gate.PROBLEM_BLOCK)] == [2, 5]
    assert gate.premise_gaps(_issue(body + _CHANGE), build=True, created_at=_NEW)[1]
    assert _run_plan(tmp_path, _plan(body + _CHANGE), epoch="2026-11-15T00:00:00Z").returncode == 2


def test_the_label_strip_takes_only_a_leading_run():
    assert not gate._plan_gate.field_empty("TBD Shape: later")
    assert gate._plan_gate.field_empty("Cost: Stated: Wrong fix:")


# -- the shipped epoch (PR 2c): what setting FORM_EPOCH changes ----------------------

#: The constant as shipped, read before any fixture pins it.
_SHIPPED = gate._plan_gate.FORM_EPOCH
_BEFORE_SHIP = "2026-11-16T00:00:00Z"
_AFTER_SHIP = "2026-12-01T00:00:00Z"
_VERIFIED = """## Verification
1. Reproduced on main at abc123 with `genesis status`.
2. It printed `KeyError: 'x'`.
3. Ruled out: config is the default, the install is current.
4. Searched: no issue for this, no fix on main.
5. Expected a status table; got a traceback.
"""


@pytest.fixture
def shipped(monkeypatch):
    monkeypatch.setattr(gate._plan_gate, "FORM_EPOCH", _SHIPPED)


def test_the_shipped_epoch_is_set_and_written_as_github_writes_created_at():
    import re

    assert _SHIPPED is not None
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", _SHIPPED)
    assert _BEFORE_SHIP < _SHIPPED < _AFTER_SHIP


def test_shipped_devin_ready_on_a_complete_old_six_names_the_conversion(shipped):
    plan = _devin(_issue(_OLD_SIX), created=_OLD)
    assert plan.remove == ["devin-ready"]
    assert plan.comment.startswith("(aside)")
    assert gate.CONVERSION_NOTE in plan.comment and "9 (Shape)" in plan.comment


def test_shipped_devin_ready_on_a_complete_nine_stays(shipped):
    plan = _devin(_issue(_PROBLEM + _CHANGE), created=_OLD)
    assert not plan.remove and not plan.comment


def test_shipped_issue_owes_the_new_form_by_its_creation_date(shipped):
    assert _opened(_issue(_OLD_SIX), created=_BEFORE_SHIP).add == []
    assert _opened(_issue(_OLD_SIX), created=_AFTER_SHIP).add == ["premises-missing"]
    assert _opened(_issue(_PROBLEM), created=_AFTER_SHIP).add == []
    msg = gate.owed_message({"body": _issue(_OLD_SIX), "labels": [], "created_at": _AFTER_SHIP})
    assert gate.CONVERSION_NOTE in msg


def test_shipped_bug_label_switches_forms_only_before_the_epoch(shipped):
    bug = {"labels": ["bug"]}
    before = gate.decide(_event("opened", _VERIFIED, labels=["bug"], created=_BEFORE_SHIP))
    after = gate.decide(_event("opened", _VERIFIED, labels=["bug"], created=_AFTER_SHIP))
    assert before.add == [] and after.add == ["premises-missing"]
    assert gate.owed_answers({**bug, "body": _VERIFIED, "created_at": _AFTER_SHIP})[0] == "premise"
    problem = _issue(_PROBLEM)
    assert gate.decide(_event("opened", problem, labels=["bug"], created=_AFTER_SHIP)).add == []
    old = gate.decide(_event("opened", problem, labels=["bug"], created=_BEFORE_SHIP))
    assert old.add == ["premises-missing"]  # before the epoch a bug owes Verification


def test_shipped_bug_form_reports_stay_exempt(shipped):
    form = gate._bug_form_headings()
    body = "\n\n".join(f"{h}\n\nsome text" for h in form)
    assert gate.is_bug_form(body)
    assert gate.decide(_event("opened", body, labels=["bug"], created=_AFTER_SHIP)).add == []


def test_shipped_check_reads_ghs_createdAt_for_the_bug_label(shipped, capsys):
    body = _issue(_PROBLEM + _CHANGE) + "\n" + _VERIFIED.replace(
        "2. It printed `KeyError: 'x'`.", "2. TBD"
    )
    assert "2. TBD" in body
    after = {"body": body, "labels": [{"name": "bug"}], "createdAt": _AFTER_SHIP}
    assert gate.check(7, view=lambda n: after) == 0
    assert "missing verification answer" not in capsys.readouterr().out
    before = {**after, "createdAt": _BEFORE_SHIP}
    assert gate.check(7, view=lambda n: before) == 0
    assert "missing verification answer 2" in capsys.readouterr().out


def test_shipped_check_on_an_old_six_names_the_conversion(shipped, capsys):
    view = {"body": _issue(_OLD_SIX), "labels": [], "createdAt": _OLD}
    assert gate.check(7, view=lambda n: view) == 1
    assert gate.CONVERSION_NOTE in capsys.readouterr().out


def test_shipped_plan_gate_blocks_an_old_six_and_passes_the_nine(tmp_path):
    old = _run_plan(tmp_path, _plan(_OLD_SIX), epoch=_SHIPPED)
    assert old.returncode == 2 and "six-question form" in old.stderr
    assert _run_plan(tmp_path, _plan(_PROBLEM + _CHANGE), epoch=_SHIPPED).returncode == 0


def _feature_form_value() -> str:
    import yaml

    form = yaml.safe_load((_ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.yml").read_text())
    field = next(b for b in form["body"] if b.get("id") == "premises")
    assert "render" not in field["attributes"]  # a render: fences the answers as code
    return field["attributes"]["value"]


def _rendered(premises: str) -> str:
    """The issue body GitHub renders from the feature form: each field under its
    ``### <label>`` heading."""
    return f"### Problem or motivation\n\nx\n\n### Premise checks\n\n{premises}"


def test_the_feature_form_prefill_parses_as_the_labelled_form(shipped):
    value = _feature_form_value()
    answers = gate.premise_answers(_rendered(value))
    assert [answers[n].split(":", 1)[0] for n in range(1, 10)] == [
        label for label, _ in gate._plan_gate.FORM
    ]
    # Untouched, the prefill is labels only: every Problem answer is still owed,
    # and the bare Change labels do not make the Change block "written".
    assert _gaps(_rendered(value), created=_AFTER_SHIP) == [1, 2, 3, 4, 5]
    filled = (
        value.replace("Observed:", "Observed: it fails on main abc123, not a stale tree.")
        .replace("Intended:", "Intended: it passes.")
        .replace("Stated:", "Stated: docs/x.md:40")
        .replace("Cost:", "Cost: every PR, counted from CI.")
        .replace("Searched:", "Searched: issues and main; nothing.")
        .replace("Acceptance:", "Acceptance: a merged migration passes.")
    )
    assert _gaps(_rendered(filled), created=_AFTER_SHIP) == [5]  # Wrong fix: still empty
    done = filled.replace("Wrong fix:", "Wrong fix: pinning the base to HEAD.")
    assert _gaps(_rendered(done), created=_AFTER_SHIP) == []


# -- the plan gate's mechanics on the path plans take now (shipped epoch) ----------
# test_plan_premise_gate.py pins these mechanics in the six-question form; the
# reader is shared, so these cover the nine-item path through the same gate.

_NINE = _PROBLEM + _CHANGE


def _nine_plan(answers: str = _NINE) -> str:
    return _plan(answers)


@pytest.mark.parametrize("n", range(1, 10))
def test_shipped_each_missing_plan_answer_is_named(tmp_path, n):
    answers = "".join(ln + "\n" for ln in _NINE.splitlines() if not ln.startswith(f"{n}. "))
    r = _run_plan(tmp_path, _nine_plan(answers), epoch=_SHIPPED)
    assert r.returncode == 2
    if n == 1:  # the form is told from answer 1, so without it the note says so
        assert "its first answer must start `Observed:`" in r.stderr
    else:
        assert f"answer {n} (" in r.stderr


@pytest.mark.parametrize("empty", ["", "n/a", "TBD", "<the alternatives>", "**N/A**"])
def test_shipped_an_empty_plan_answer_is_blocked(tmp_path, empty):
    answers = _NINE.replace(
        "7. Alternatives: the existing base helper decides this; reused.", f"7. Alternatives: {empty}"
    )
    r = _run_plan(tmp_path, _nine_plan(answers), epoch=_SHIPPED)
    assert r.returncode == 2 and "answer 7 (" in r.stderr


@pytest.mark.parametrize("heading", ["## Premise questions", "#### Premise checks", "## **Premise checks**"])
def test_shipped_heading_spellings_and_paren_numbering_pass(tmp_path, heading):
    plan = _nine_plan().replace("## Premise checks", heading)
    for n in range(1, 10):
        plan = plan.replace(f"\n{n}. ", f"\n{n}) ")
    assert _run_plan(tmp_path, plan, epoch=_SHIPPED).returncode == 0


def test_shipped_a_malformed_first_answer_is_not_called_a_six(tmp_path):
    answers = _NINE.replace("1. Observed: the guard", "1. Observed — the guard")
    r = _run_plan(tmp_path, _nine_plan(answers), epoch=_SHIPPED)
    assert r.returncode == 2 and "not in the nine-item form" in r.stderr
    assert "must start `Observed:`" in r.stderr


# -- the devin-ready guard's mechanics on the shipped path ---------------------------


def test_shipped_devin_ready_missing_one_answer_is_removed_naming_it(shipped):
    body = _issue(_NINE.replace("8. Evidence: the measured CI runs above.", "8. Evidence: TBD"))
    plan = _devin(body, created=_OLD)
    assert plan.remove == ["devin-ready"] and plan.comment.startswith("(aside)")
    assert "1 of 9" in plan.comment and "8. Evidence" in plan.comment
    assert gate.CONVERSION_NOTE not in plan.comment  # a nine-item section is not told to convert


def test_shipped_devin_ready_is_not_commented_twice(shipped):
    body = _issue(_PROBLEM)
    event = _event("labeled", body, labels=[], label="devin-ready", created=_OLD)
    assert gate.decide(event) == gate.Plan()  # already removed: no second comment


def test_shipped_a_verified_bug_bound_for_devin_owes_the_nine(shipped):
    plan = gate.decide(
        _event("labeled", _VERIFIED, labels=["bug", "devin-ready"], label="devin-ready", created=_OLD)
    )
    assert plan.remove == ["devin-ready"] and "of 9" in plan.comment


def test_shipped_owed_message_tells_a_bug_filer_why_verification_is_not_enough(shipped):
    msg = gate.owed_message({"body": _VERIFIED, "labels": ["bug"], "created_at": _AFTER_SHIP})
    assert "owes the Problem block, not Verification" in msg and "items 1, 2 and 4" in msg


def test_setting_the_epoch_back_to_none_restores_the_either_form_rule(monkeypatch, tmp_path):
    """The rollback lever: with FORM_EPOCH None again, a complete six passes the
    label job, the devin-ready guard and the plan gate, and so does the nine."""
    monkeypatch.setattr(gate._plan_gate, "FORM_EPOCH", None)
    for body in (_OLD_SIX, _PROBLEM + _CHANGE):
        assert _opened(_issue(body), created=_AFTER_SHIP).add == []
        plan = _devin(_issue(body), created=_AFTER_SHIP)
        assert not plan.remove and not plan.comment
    payload = {"tool_name": "ExitPlanMode", "tool_input": {"plan": _plan(_OLD_SIX)}}
    assert gate._plan_gate._check(payload) == 0


def test_an_as_line_plan_with_unlabelled_change_answers_is_told_to_label_them(tmp_path):
    answers = "1. Problem: as #42\n6. reused the shared reader.\n7. none simpler.\n8. cited.\n9. 40 lines.\n"
    r = _run_plan(tmp_path, _plan(answers))
    assert r.returncode == 2 and "the other answers with their own labels" in r.stderr


# -- 2b: a proposal (answer 2 says Stated: nowhere) --------------------------------

_CITED = _PROBLEM + _CHANGE


def _nowhere(stated: str) -> str:
    return _issue(_CITED.replace("Stated: docs/x.md:40", f"Stated: {stated}"))


@pytest.mark.parametrize(
    "stated", ["nowhere", '"nowhere"', "“nowhere”", "**nowhere**", "Nowhere.", "nowhere yet", "nowhere — new"]
)
def test_a_stated_nowhere_is_a_proposal(stated):
    answers = gate.premise_answers(gate.strip_quoted(_nowhere(stated)))
    assert gate._plan_gate.is_proposal(answers)


@pytest.mark.parametrize(
    "body",
    [
        _issue(_CITED),
        # prose in the main answer is not the Stated field
        _issue(_CITED.replace("2. Intended: a migration", "2. Intended: found nowhere else, a migration")),
        _issue(_OLD_SIX),  # an old six has no Stated field
        _nowhere("none — new behaviour"),  # the stated residual: only the word the form names
        _nowhere("docs/x.md:40, found nowhere else"),  # a citation first: the match is anchored
        _nowhere("<nowhere>"),  # a placeholder, which CommonMark drops
        _issue(
            _CITED.replace(
                "2. Intended: a migration merged after checkout is not reported. Stated: docs/x.md:40",
                gate._plan_gate._form_template().splitlines()[1].strip(),
            )
        ),  # the template's own slot
    ],
)
def test_a_cited_or_old_form_issue_is_not_a_proposal(body):
    assert not gate._plan_gate.is_proposal(gate.premise_answers(gate.strip_quoted(body)))


def test_the_label_job_adds_and_removes_proposal_with_the_body(shipped):
    assert gate.decide(_event("opened", _nowhere("nowhere"))).add == ["proposal"]
    edited = gate.decide(_event("edited", _issue(_CITED), labels=["proposal"]))
    assert edited.remove == ["proposal"] and edited.add == []
    kept = gate.decide(_event("edited", _nowhere('"nowhere"'), labels=["proposal"]))
    assert kept.add == [] and kept.remove == []


def test_devin_ready_on_a_complete_proposal_is_removed_saying_how_to_clear_it(shipped):
    plan = _devin(_nowhere("nowhere"), created=_OLD)
    assert plan.remove == ["devin-ready"]
    assert plan.comment.startswith("(aside)\n")
    assert "this issue is a proposal" in plan.comment and gate.PROPOSAL_LINE in plan.comment
    assert "Missing or empty" not in plan.comment and "missing premise answers" not in plan.comment
    assert plan.comment.rstrip().endswith("Then apply `devin-ready` again.")
    assert plan.add == ["proposal"]  # the guard brings the mirror label up to date too


def test_devin_ready_on_a_proposal_with_missing_answers_names_both(shipped):
    body = _nowhere("nowhere").replace("8. Evidence: the measured CI runs above.", "8. Evidence: TBD")
    plan = _devin(body, created=_OLD)
    assert "missing premise answers and this issue is a proposal" in plan.comment
    assert "1 of 9" in plan.comment and gate.PROPOSAL_LINE in plan.comment


def test_only_the_body_decides_never_the_label(shipped):
    hand_removed = _event("labeled", _nowhere("nowhere"), labels=["devin-ready"], label="devin-ready")
    assert gate.decide(hand_removed).remove == ["devin-ready"]
    hand_added = _event(
        "labeled", _issue(_CITED), labels=["devin-ready", "proposal"], label="devin-ready"
    )
    plan = gate.decide(hand_added)
    assert "devin-ready" not in plan.remove and plan.remove == ["proposal"] and not plan.comment


def test_a_failed_removal_on_a_proposal_says_so():
    plan = gate.Plan(remove=["devin-ready"], comment=gate._comment([], proposal=True))
    calls, bodies = [], []

    def run(args):
        calls.append(args)
        if args[:2] == ["issue", "comment"]:
            bodies.append(Path(args[-1]).read_text())
        return 1 if "--remove-label" in args else 0

    gate.apply(plan, 7, run)
    assert "could NOT be removed" in bodies[0] and "was removed" not in bodies[0]
    assert "this issue is a proposal" in bodies[0]


def test_apply_creates_the_proposal_label_before_adding_it():
    calls = []
    gate.apply(gate.Plan(add=["proposal"]), 7, lambda args: calls.append(args) or 0)
    assert calls[0][:3] == ["label", "create", "proposal"]
    assert calls[1] == ["issue", "edit", "7", "--add-label", "proposal"]


@pytest.mark.parametrize("stated", ["docs/x.md:40", "nowhere"])
@pytest.mark.parametrize("drop", [None, "8. Evidence: the measured CI runs above."])
def test_check_and_the_guard_agree_on_a_proposal(shipped, capsys, stated, drop):
    body = _nowhere(stated)
    if drop:
        body = body.replace(drop, "8. Evidence: TBD")
    guard_removes = "devin-ready" in _devin(body, created=_OLD).remove
    code = gate.check(7, view=lambda n: {"body": body, "labels": [], "createdAt": _OLD})
    out = capsys.readouterr().out
    assert (code == 1) == guard_removes, (stated, drop)
    assert ("a proposal" in out) == (stated == "nowhere")


def test_a_devin_ready_comment_needs_a_reason():
    with pytest.raises(ValueError):
        gate._comment([])
