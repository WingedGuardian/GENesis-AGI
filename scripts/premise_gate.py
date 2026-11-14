#!/usr/bin/env python3
"""Issue premise gate: an issue carries its answers, and `devin-ready` does not stay on one without them.

``.claude/docs/premise-check.md`` ("At plan and issue time") asks every issue that
specifies work to answer six questions. The ``devin-ready`` label starts a PAID
Devin session; MEASURED 2026-10-28, of the 7 issues labelled ``devin-ready``
since the sixth question was added (2026-10-06), 4 carried no premise section.

Run by ``.github/workflows/premise-gate.yml`` on ``issues`` events. Two jobs:

1. **Label.** On opened, edited and reopened, and when ``bug`` is added or
   removed, an issue without its OWN answers gets ``premises-missing``, and loses
   it once they are complete. That label has one meaning everywhere: this
   issue's own section is incomplete. A work issue owes
   the six premise answers (``## Premise checks``). A bug report (labelled
   ``bug``) owes the five verification answers instead (``## Verification``:
   reproduced, evidence, reporter-side causes ruled out, searched, expected vs
   actual), because a bug report's question is whether the bug is real, not how
   to build a change. This applies to ALL issues (owner rulings 2026-10-28;
   MEASURED the same day, 396 of the 399 issues opened since 2026-10-06 carry no
   answers, so this labels most new issues by design). Two exemptions: issues
   created before ``GATE_EPOCH`` are
   grandfathered, and bug reports filed through the bug-report form are exempt
   (owner ruling). For both, the label is never ADDED, though a label an issue
   already carries is still removed once its answers are complete.
2. **Guard.** When ``devin-ready`` is applied to an issue without the SIX
   premise answers, it is removed and a comment names the missing ones. Devin
   builds a change, so it always needs the six, bug report or not (owner ruling
   2026-10-28); a verified bug report bound for Devin carries both sections. The
   comment's first line is ``(aside)``, because a non-aside comment on a Devin
   issue can start a paid session. Removing ``devin-ready`` ignores
   grandfathering: the label event is the paid trigger, whenever the issue was
   written. ``premises-missing`` follows the label rule above, exemptions
   included (one shared ``_may_add``), so the two jobs never disagree about it.
   The guard acts only while ``devin-ready`` is still on the issue, so a re-run
   posts no second comment (two runs overlapping in time still can, inside the
   race accepted below).

``python3 scripts/premise_gate.py check <issue>`` is the read-only check a
session runs BEFORE applying ``devin-ready`` (exit 0 ready, 1 answers missing,
2 unreadable): the guard acts only after the paid label event.

The answers are read by the plan reader's own parser
(``scripts/hooks/plan_doc.premise_answers`` with the plan gate's emptiness rule),
so a plan and an issue are held to one format. Both forms are read
(``premise_gaps``): the six questions, and the nine-item form, whose Problem
block (items 1 to 5) an issue owes and whose nine a paid build owes. While
``plan_premise_gate.FORM_EPOCH`` is unset a complete six still passes
everywhere, so no issue is newly labelled and no ``devin-ready`` stripped.

Residual, accepted by the owner and stated rather than hidden: Devin's own
integration watches the label, so removing ``devin-ready`` can come after a
session has already started. The visible label and comment are the control.

The issue body is untrusted input. It is read from the event file and handed to
the parser and to ``gh`` only as argv list items or a file, never through a shell.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "hooks"))
import plan_premise_gate as _plan_gate  # noqa: E402
from plan_doc import premise_answers, strip_quoted  # noqa: E402
from plan_premise_gate import _QUESTIONS, _empty, _problem  # noqa: E402

MISSING_LABEL = "premises-missing"
DEVIN_LABEL = "devin-ready"
BUG_LABEL = "bug"
#: A bug report answers whether the bug is REAL, not how to build a change (owner
#: ruling 2026-10-28): each answer closes a way a false report happens.
VERIFICATION_TITLE = re.compile(r"verification\b", re.IGNORECASE)
VERIFICATION_QUESTIONS = (
    "reproduced: how, and on which commit (a fresh main, not a stale checkout)",
    "evidence: the exact command and its output",
    "reporter-side causes ruled out: misuse, local config, a stale tree, credentials",
    "searched: no existing issue, and no fix already on main",
    "expected vs actual",
)
#: Job 1 never adds the label to an issue created before this instant, so the
#: backlog is not labelled. Issues opened between this instant and the gate's
#: merge were opened before the workflow existed: they get the label on their
#: next edit, not at opening.
GATE_EPOCH = "2026-10-28T00:00:00Z"
#: The bug-report form. The headings of its STABLE fields (``BUG_FORM_FIELDS``)
#: are read from the form itself, so new reports track a renamed field, and adding
#: a field cannot un-exempt the reports filed before it. Renaming one of these
#: fields DOES un-exempt reports filed before the rename (their bodies carry the
#: old heading), so rename them only with that in mind.
BUG_FORM = Path(__file__).resolve().parents[1] / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml"
BUG_FORM_FIELDS = ("description", "steps")


def _bug_form_headings(path: Path = BUG_FORM) -> list[str]:
    """The ``### <label>`` headings GitHub renders for the bug form's stable
    fields, or [] when the form cannot be read (then nothing is exempted)."""
    try:
        import yaml  # noqa: PLC0415

        form = yaml.safe_load(path.read_text(encoding="utf-8"))
        return [
            f"### {item['attributes']['label']}"
            for item in form.get("body", [])
            if isinstance(item, dict)
            and item.get("id") in BUG_FORM_FIELDS
            and isinstance((item.get("attributes") or {}).get("label"), str)
        ]
    except Exception:  # noqa: BLE001 — an unreadable form exempts nothing
        return []


def is_bug_form(body: str, headings: Sequence[str] | None = None) -> bool:
    """Whether ``body`` was filed through the bug form.

    Judged on where the form PUTS its headings, not on how the body renders: the
    body opens with the form's first heading, and every heading sits on a line of
    its own. Raw lines, deliberately: an unclosed fence or HTML comment that a
    reporter left in the Description would otherwise swallow the later headings
    and drop a real report's exemption. A work issue that merely quotes the
    headings in an example does not open with them, so it is not exempt.
    """
    wanted = _bug_form_headings() if headings is None else list(headings)
    raw = [line.rstrip() for line in (body or "").splitlines()]
    first = next((line for line in raw if line), "")
    return bool(wanted) and first == wanted[0] and all(h in set(raw) for h in wanted)


@dataclass
class Plan:
    """What to do to one issue. Empty means leave it alone."""

    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    comment: str = ""


def missing_answers(body: str, *, bug: bool = False) -> list[int]:
    """The answer numbers that are missing or empty, all of them without a section.

    A work issue answers the six premise questions (``## Premise checks``); a bug
    report answers the five verification questions (``## Verification``).
    """
    if bug:
        answers = premise_answers(strip_quoted(body or ""), VERIFICATION_TITLE)
        count = len(VERIFICATION_QUESTIONS)
    else:
        answers = premise_answers(strip_quoted(body or ""))
        count = len(_QUESTIONS)
    if answers is None:
        return list(range(1, count + 1))
    return [n for n in range(1, count + 1) if _empty(answers.get(n, ""))]


def _written(n: int, answer: str) -> str:
    """What answer ``n`` says after its nine-item label (the whole answer when the
    label is absent), so a bare ``Mechanism:`` or one left as the template's
    placeholder reads as empty."""
    after = _plan_gate._after_label(_plan_gate.FORM[n - 1][0], answer)
    return answer if after is None else after


def premise_gaps(
    body: str, *, build: bool, created_at: str = ""
) -> tuple[tuple[str, ...], list[int], dict[int, str]]:
    """``(questions, missing, why)`` for the premise section of ``body``.

    Two forms are read: the six questions and the nine-item form
    (``plan_premise_gate.FORM``), told apart by the section's first answer. A
    section in the nine-item form is judged by it, whenever it was written.
    Otherwise the six apply, unless the nine-item form is REQUIRED: from
    ``FORM_EPOCH``, for an issue created on or after it and for every paid build
    (``build``; the ``devin-ready`` guard ignores grandfathering). While the
    epoch is unset nothing requires it, so a complete six still passes.

    ``build`` owes all nine; an issue owes the Problem block (1 to 5), and the
    Change block too if any of it is written. ``why`` holds a reason for an
    answer that is present but malformed; a missing answer has none.
    """
    answers = premise_answers(strip_quoted(body or ""))
    epoch = _plan_gate.form_epoch()
    new_form = _plan_gate.is_new_form(answers)
    required = epoch is not None and (build or created_at >= epoch)
    old_missing = missing_answers(body)
    if epoch is None and not old_missing:
        # Until the epoch, EITHER complete form passes (spec, PR 2a): six
        # non-empty answers pass as they do today, whatever form they look
        # like, so the nine-item rules only loosen before then.
        return _QUESTIONS, [], {}
    if not new_form and not required:
        return _QUESTIONS, old_missing, {}
    change_written = new_form and any(
        not _empty(_written(n, answers.get(n, ""))) for n in _plan_gate.CHANGE_BLOCK
    )
    need = _plan_gate.PROBLEM_BLOCK
    if build or change_written:
        need = _plan_gate.PROBLEM_BLOCK + _plan_gate.CHANGE_BLOCK
    if not new_form:
        return _plan_gate.FORM_QUESTIONS, list(need), {}  # owes the nine-item form, has none
    found = _plan_gate.form_problems(answers, need)
    return _plan_gate.FORM_QUESTIONS, [n for n, _ in found], dict(found)


def _label_names(issue: dict) -> set[str]:
    """Label names, from objects (``{"name": ...}``) or plain strings."""
    names = set()
    for lbl in issue.get("labels") or []:
        name = lbl.get("name") if isinstance(lbl, dict) else lbl
        if isinstance(name, str):
            names.add(name)
    return names


def _sync_missing_label(plan: Plan, issue: dict, labels: set[str], *, may_add: bool) -> None:
    """Bring ``premises-missing`` in line with the issue's OWN section: the
    verification answers for a bug report, the six premise answers otherwise."""
    if BUG_LABEL in labels:
        missing = missing_answers(issue.get("body") or "", bug=True)
    else:
        _, missing, _ = premise_gaps(
            issue.get("body") or "", build=False, created_at=issue.get("created_at") or ""
        )
    if missing and MISSING_LABEL not in labels and may_add:
        plan.add.append(MISSING_LABEL)
    elif not missing and MISSING_LABEL in labels:
        plan.remove.append(MISSING_LABEL)


def _may_add(issue: dict) -> bool:
    """Whether the gate may newly ADD ``premises-missing`` to this issue: created
    at or after ``GATE_EPOCH``, and not a bug-form report. The ONE rule both jobs
    use, so they never disagree about an exempt issue."""
    new = (issue.get("created_at") or "") >= GATE_EPOCH
    return new and not is_bug_form(issue.get("body") or "")


def owed_answers(issue: dict) -> tuple[str, list[int]]:
    """What the label job would hold against ``issue`` were it opened now.

    Returns ``("premise" | "verification", missing)`` when an ``opened`` event would
    add ``premises-missing``, else ``("", [])``: the issue is complete or exempt.
    Built only from ``_may_add``, ``_label_names``, ``missing_answers`` and
    ``premise_gaps``, the
    pieces ``decide`` uses, so a check run before filing (the
    ``hooks/issue_premise_gate.py`` PreToolUse hook) agrees with the gate that runs
    after it. A test pins that agreement cell by cell, at one commit: the hook runs
    the local copy, the Action the default branch's.

    It adds no module, deliberately: the Action sparse-checks out exactly five
    files, so a new module here would fail every run there. ``issue`` needs
    ``body``, ``labels`` and ``created_at``; a caller filing a new issue passes the
    current UTC time.
    """
    if not _may_add(issue):
        return "", []
    bug = BUG_LABEL in _label_names(issue)
    if bug:
        missing = missing_answers(issue.get("body") or "", bug=True)
    else:
        _, missing, _ = premise_gaps(
            issue.get("body") or "", build=False, created_at=issue.get("created_at") or ""
        )
    return ("verification" if bug else "premise", missing) if missing else ("", [])


def owed_message(issue: dict) -> str:
    """The fix for what ``owed_answers`` reports, written for the agent, or ""."""
    section, missing = owed_answers(issue)
    if not section:
        return ""
    bug = section == "verification"
    heading = "## Verification" if bug else "## Premise checks"
    reasons: dict[int, str] = {}
    if bug:
        questions = VERIFICATION_QUESTIONS
    else:
        questions, _, reasons = premise_gaps(
            issue.get("body") or "", build=False, created_at=issue.get("created_at") or ""
        )
    body = strip_quoted(issue.get("body") or "")
    answers = premise_answers(body, VERIFICATION_TITLE) if bug else premise_answers(body)
    if answers is None:
        why = " (the issue is labelled `bug`)" if bug else ""
        problems = [f"the body has no `{heading}` section{why}"]
    elif questions is _QUESTIONS or bug:
        problems = [_problem(n, questions[n - 1], answers.get(n, "")) for n in missing]
    else:
        problems = [
            f"answer {n} ({questions[n - 1].split(':', 1)[0]}) "
            + reasons.get(n, "is missing: this issue owes the nine-item form")
            for n in missing
        ]
    return (
        "".join(f"  - {p}\n" for p in problems)
        + "\nAdd or complete this section in the body, then run the command again:\n"
        + f"\n  {heading}\n"
        + (
            _plan_gate._form_template()
            if questions is _plan_gate.FORM_QUESTIONS
            else "".join(f"  {n}. <{q}>\n" for n, q in enumerate(questions, start=1))
        )
        + "\n`n/a — <reason>` is a valid answer; a bare `n/a`, `TBD` or `<placeholder>` "
        'is not. Questions: .claude/docs/premise-check.md, "At plan and issue time".\n'
    )


def decide(event: dict) -> Plan:
    """The label and comment changes for one ``issues`` event payload."""
    action = event.get("action") or ""
    issue = event.get("issue") or {}
    if issue.get("pull_request") is not None:
        return Plan()
    labels = _label_names(issue)
    changed = (event.get("label") or {}).get("name")
    plan = Plan()

    if action == "labeled" and changed == DEVIN_LABEL:
        if DEVIN_LABEL not in labels:
            return plan  # already removed (a re-run, or another guard run): no second comment
        # The paid trigger always needs the premise answers that specify a
        # change, bug or not: the six, or all nine of the nine-item form.
        questions, build_missing, why = premise_gaps(issue.get("body") or "", build=True)
        if build_missing:
            plan.remove.append(DEVIN_LABEL)
            plan.comment = _comment(build_missing, questions, why)
        _sync_missing_label(plan, issue, labels, may_add=_may_add(issue))
        return plan

    relabelled_bug = action in ("labeled", "unlabeled") and changed == BUG_LABEL
    if action not in ("opened", "edited", "reopened") and not relabelled_bug:
        return plan
    _sync_missing_label(plan, issue, labels, may_add=_may_add(issue))
    return plan


REMOVED_LINE = f"`{DEVIN_LABEL}` was removed: this issue is missing premise answers"
NOT_REMOVED_LINE = (
    f"`{DEVIN_LABEL}` could NOT be removed automatically; remove it by hand before a "
    "Devin session starts: this issue is missing premise answers"
)


def _comment(
    missing: Sequence[int],
    questions: Sequence[str] = _QUESTIONS,
    why: dict[int, str] | None = None,
) -> str:
    why = why or {}
    rows = "".join(
        f"{n}. {questions[n - 1]}" + (f" ({why[n]})" if n in why else "") + "\n" for n in missing
    )
    return (
        "(aside)\n\n"
        f"{REMOVED_LINE}, so it is not "
        "ready for a paid build session.\n\n"
        f"Missing or empty ({len(missing)} of {len(questions)}):\n\n{rows}\n"
        f"Add a `## Premise checks` section with all {len(questions)} numbered answers "
        "(`n/a — <reason>` counts), then apply the label again. Format: "
        '`.claude/docs/premise-check.md`, "At plan and issue time".\n'
    )


Runner = Callable[[list[str]], int]


def _gh(args: list[str]) -> int:
    return subprocess.run(["gh", *args], check=False).returncode


def apply(plan: Plan, number: int, run: Runner = _gh) -> int:
    """Carry out ``plan`` with ``gh``. Every step is attempted; returns the
    largest exit code.

    Removing ``devin-ready`` goes FIRST and on its own, so no later or cosmetic
    step (creating the label, the comment) can stand between a paid trigger and
    its removal. Creating ``premises-missing`` fails when the label already
    exists, which is the usual case, so its exit code is ignored: if the label
    really is missing, the add that follows fails and that failure is reported.
    """
    codes = []
    comment = plan.comment
    if DEVIN_LABEL in plan.remove:
        removed = run(["issue", "edit", str(number), "--remove-label", DEVIN_LABEL])
        codes.append(removed)
        if removed != 0:
            comment = comment.replace(REMOVED_LINE, NOT_REMOVED_LINE, 1)
    if MISSING_LABEL in plan.add:
        run(
            [
                "label",
                "create",
                MISSING_LABEL,
                "--color",
                "D93F0B",
                "--description",
                "Premise answers missing; see .claude/docs/premise-check.md",
            ]
        )
    edit: list[str] = []
    for name in plan.add:
        edit += ["--add-label", name]
    for name in plan.remove:
        if name != DEVIN_LABEL:
            edit += ["--remove-label", name]
    if edit:
        codes.append(run(["issue", "edit", str(number), *edit]))
    if comment:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as fh:
            fh.write(comment)
        try:
            codes.append(run(["issue", "comment", str(number), "--body-file", fh.name]))
        finally:
            os.unlink(fh.name)
    return max((abs(c) for c in codes), default=0)


Viewer = Callable[[int], "dict | None"]


def _view(number: int) -> dict | None:
    """The issue's CURRENT body, labels and creation time, or None if gh fails."""
    try:
        out = subprocess.run(
            ["gh", "issue", "view", str(number), "--json", "body,labels,createdAt"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:  # gh missing or not executable: unreadable, not "no answers"
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except ValueError:
        return None


def live(event: dict, view: Viewer = _view) -> dict:
    """``event`` with its issue's body and labels replaced by their current values.

    The event carries a snapshot from when it fired; a run that waited behind
    another for the same issue would otherwise act on a stale body. When the
    read fails, the snapshot is used.
    """
    issue = event.get("issue") or {}
    number = issue.get("number")
    current = view(number) if isinstance(number, int) else None
    if not isinstance(current, dict):
        return event
    merged = dict(issue)
    merged["body"] = current.get("body") or ""
    # A read with no label LIST tells us nothing about the labels: keep the
    # event's, so the guard never mistakes "unknown" for "devin-ready is gone".
    live_labels = current.get("labels")
    merged["labels"] = live_labels if isinstance(live_labels, list) else issue.get("labels") or []
    merged["created_at"] = current.get("createdAt") or issue.get("created_at")
    return {**event, "issue": merged}


def check(number: int, view: Viewer = _view) -> int:
    """Read-only: whether issue ``number`` is ready for ``devin-ready``.

    Run it BEFORE applying the label. Prints the missing premise answers (and,
    for a ``bug`` issue, the missing verification answers). Returns 0 when the
    premise answers a paid build owes are complete (``premise_gaps`` with
    ``build``), 1 when any is missing, 2 when the issue cannot be read. Only
    those decide the code, because only they decide whether the guard removes
    the label.
    """
    issue = view(number)
    if not isinstance(issue, dict):
        print(f"premise_gate: issue {number}: could not be read", file=sys.stderr)
        return 2
    body = issue.get("body") or ""
    questions, missing, _ = premise_gaps(body, build=True)
    for n in missing:
        print(f"missing premise answer {n}: {questions[n - 1]}")
    if BUG_LABEL in _label_names(issue):
        for n in missing_answers(body, bug=True):
            print(f"missing verification answer {n}: {VERIFICATION_QUESTIONS[n - 1]}")
    if missing:
        print(
            f"premise_gate: issue {number}: NOT ready for {DEVIN_LABEL} "
            f"({len(missing)} of {len(questions)} missing)"
        )
        return 1
    print(f"premise_gate: issue {number}: ready for {DEVIN_LABEL}")
    return 0


def main(argv: Sequence[str] | None = None, run: Runner = _gh, view: Viewer = _view) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["check"]:
        number = re.fullmatch(r"#?([0-9]+)", args[1]) if len(args) == 2 else None
        if number is None:
            print("usage: premise_gate.py check <issue-number>", file=sys.stderr)
            return 2
        return check(int(number.group(1)), view)
    path = args[0] if args else os.environ.get("GITHUB_EVENT_PATH", "")
    if not path:
        print("premise_gate: no event file", file=sys.stderr)
        return 2
    event = live(json.loads(Path(path).read_text(encoding="utf-8")), view)
    number = (event.get("issue") or {}).get("number")
    plan = decide(event)
    if not isinstance(number, int) or not (plan.add or plan.remove or plan.comment):
        print(f"premise_gate: issue {number}: no change")
        return 0
    print(
        f"premise_gate: issue {number}: add={plan.add} remove={plan.remove} comment={bool(plan.comment)}"
    )
    return apply(plan, number, run)


if __name__ == "__main__":
    sys.exit(main())
