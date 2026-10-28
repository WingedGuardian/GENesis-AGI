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
   guard also brings ``premises-missing`` in line with the label rule above, so
   the two jobs never disagree about it. That comment's first line is ``(aside)``, because a non-aside comment
   on a Devin issue can start a paid session. This job has no grandfathering:
   the label event is the paid trigger, whenever the issue was written.

The answers are read by the plan reader's own parser
(``scripts/hooks/plan_doc.premise_answers`` with the plan gate's emptiness rule),
so a plan and an issue are held to one format.

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
from plan_doc import premise_answers, strip_quoted  # noqa: E402
from plan_premise_gate import _QUESTIONS, _empty  # noqa: E402

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
#: are read from the form itself, so renaming one cannot silently break the
#: exemption, and adding a field cannot un-exempt the reports filed before it.
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
    """Whether ``body`` was filed through the bug form: every field heading is present."""
    wanted = _bug_form_headings() if headings is None else list(headings)
    lines = {line.rstrip() for line in (body or "").splitlines()}
    return bool(wanted) and all(h in lines for h in wanted)


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
    missing = missing_answers(issue.get("body") or "", bug=BUG_LABEL in labels)
    if missing and MISSING_LABEL not in labels and may_add:
        plan.add.append(MISSING_LABEL)
    elif not missing and MISSING_LABEL in labels:
        plan.remove.append(MISSING_LABEL)


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
        # The paid trigger always needs the six premise answers, bug or not:
        # Devin builds a change, and the six are what specify one.
        six_missing = missing_answers(issue.get("body") or "")
        if six_missing:
            plan.remove.append(DEVIN_LABEL)
            plan.comment = _comment(six_missing)
        _sync_missing_label(plan, issue, labels, may_add=True)
        return plan

    relabelled_bug = action in ("labeled", "unlabeled") and changed == BUG_LABEL
    if action not in ("opened", "edited", "reopened") and not relabelled_bug:
        return plan
    new = (issue.get("created_at") or "") >= GATE_EPOCH
    _sync_missing_label(
        plan, issue, labels, may_add=new and not is_bug_form(issue.get("body") or "")
    )
    return plan


REMOVED_LINE = f"`{DEVIN_LABEL}` was removed: this issue is missing premise answers"
NOT_REMOVED_LINE = (
    f"`{DEVIN_LABEL}` could NOT be removed automatically; remove it by hand before a "
    "Devin session starts: this issue is missing premise answers"
)


def _comment(missing: Sequence[int]) -> str:
    rows = "".join(f"{n}. {_QUESTIONS[n - 1]}\n" for n in missing)
    return (
        "(aside)\n\n"
        f"{REMOVED_LINE}, so it is not "
        "ready for a paid build session.\n\n"
        f"Missing or empty ({len(missing)} of 6):\n\n{rows}\n"
        "Add a `## Premise checks` section with all six numbered answers "
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
    out = subprocess.run(
        ["gh", "issue", "view", str(number), "--json", "body,labels,createdAt"],
        capture_output=True,
        text=True,
        check=False,
    )
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
    merged["labels"] = current.get("labels") or []
    merged["created_at"] = current.get("createdAt") or issue.get("created_at")
    return {**event, "issue": merged}


def main(argv: Sequence[str] | None = None, run: Runner = _gh, view: Viewer = _view) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
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
