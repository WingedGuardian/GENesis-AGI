#!/usr/bin/env python3
"""A plan that specifies repo work answers the six premise questions before it is presented.

``.claude/docs/premise-check.md`` ("At plan and issue time") asks every plan that
specifies work to answer six questions. Until this hook, nothing read the answers.
MEASURED 2026-10-27 over one install's 72 plans: 59 name a repo path, and 13 carry a
premise heading, under four spellings (grep, case-insensitive,
``^#{1,6} *premise (check|checks|questions)``).

PreToolUse: ExitPlanMode. It BLOCKS (stderr + exit 2, the adopt_first_gate contract),
every time, on two facts and nothing else:

  1. the live plan names a repo path but has no premise section (a heading titled
     "Premise check(s)" or "Premise question(s)", any level);
  2. one of the six numbered answers is missing or empty (``n/a — <reason>`` is an
     answer; a bare ``n/a``, ``TBD`` or an unfilled ``<placeholder>`` is not).

Neither judges content, and the fix is one edit. What they do require is the
documented FORM: answers as a numbered list. Six answers run together on one line
read as one answer, and a plan written before question 6 existed is missing one.
Whether an answer is TRUE is the reviewer's question, not this hook's (owner ruling,
2026-10-27: no machinery that second-guesses the model). So there is no tag grammar,
no transcript corroboration, no path-existence check (an honest "no scripts/x.py
exists" must pass) and no tracker lookup (a ledger id may live on another install).

SCOPE. Only plans that name a repo path (``src/``, ``scripts/``, ``tests/``,
``.claude/``, ``docs/``, ``config/``, ``.github/``) outside code blocks are checked,
the "plan that specifies work" of the doc; a research or errand plan is out of scope.
It fires on most dev plans (59 of 72 above), and that is intended: unlike a verdict
gate, its block cannot be acknowledged away, only satisfied, so its steady cost is
one section per plan. Known misses, all fail-open: a plan that names only absolute
repo paths, directories, top-level files (``CLAUDE.md``, ``pyproject.toml``), bare
filenames or PR numbers.

Fails OPEN and never asks: an internal error or an unreadable plan lets the plan
through, and it prints nothing on stdout, so it can never volunteer a
``permissionDecision`` (on ExitPlanMode an ``allow`` would skip the user's plan
approval; ``plan_confidence_reminder``'s docstring). A block asks for work, never for
approval, so a background session can satisfy it alone.
"""

from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import read_payload, tool_input  # noqa: E402
from plan_doc import live_section, plan_path, premise_answers, strip_quoted  # noqa: E402

#: A repo-relative path with a file extension. The lookbehind keeps a home or
#: absolute path (``~/.claude/plans/x.md``) from counting as repo work.
_REPO_PATH = re.compile(
    r"(?<![\w./~-])(?:\./)?(?:src|scripts|tests|\.claude|docs|config|\.github)/[\w./-]*\w\.[A-Za-z]{1,5}\b"
)
#: No answer at all: a placeholder, not a wrong answer. ``n/a`` alone (or with a
#: dangling dash) is empty; ``n/a — <reason>`` is an answer. The issue form renders
#: an empty field as ``_No response_``. An answer that is only an unfilled template
#: slot (``<the caller>``) is empty too, without a rule here: CommonMark reads such a
#: line as an HTML block, which ``strip_quoted`` blanks.
_EMPTY = re.compile(
    r"^(?:_no response_|n/?a[.:]?(?:\s*[—–-]+)?|none\.?|tbd\.?|todo\.?|\?+|-+|—+|\.+|…)?$",
    re.IGNORECASE,
)
#: A safety bound, not a corpus figure: the largest plan on the measuring install
#: was 786 KB (checked in 1.3 s), so 8 MB leaves room for any real plan while
#: refusing a runaway file. Over it, the payload's own plan text is used instead.
_MAX_PLAN_BYTES = 8 * 1024 * 1024
_QUESTIONS = (
    "outside-behaviour claims measured or cited",
    "repo searched for existing code, result written down",
    "scope limits do not block the obvious shared code",
    "caller named and tracked, or why there is none",
    "every number says how it was measured",
    "PR shape: expected counted lines and the split or Shape: reason",
)


def _plan_text(payload: dict) -> str:
    """The plan this ExitPlanMode presents. The file the payload names first (it is
    what the user sees), then the plan text the payload carries, and only then
    ``plan_path``'s guess from disk, which can name another plan."""
    given = tool_input(payload)
    for key in ("planFilePath", "plan_file_path"):
        named = given.get(key)
        if isinstance(named, str) and named:
            try:
                return _read_regular(Path(named))
            except (OSError, ValueError):  # ValueError includes UnicodeDecodeError
                break
    plan = given.get("plan")
    if isinstance(plan, str) and plan:
        return plan
    guessed = plan_path(payload)
    return _read_regular(Path(guessed)) if guessed else ""


def _read_regular(path: Path) -> str:
    """The file's text, only for a regular file within the size cap. A FIFO would
    block the hook until its timeout and a device such as ``/dev/zero`` would grow
    memory without bound; both raise here, and the caller falls back."""
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_PLAN_BYTES:
        raise ValueError(f"not a regular file within {_MAX_PLAN_BYTES} bytes: {path}")
    return path.read_text(encoding="utf-8")


def _empty(answer: str) -> bool:
    return bool(_EMPTY.match(answer.strip()))


def _check(payload: dict) -> int:
    prose = strip_quoted(live_section(_plan_text(payload)))
    if not _REPO_PATH.search(prose):
        return 0  # no repo work named — not a plan this check is for

    answers = premise_answers(prose)
    if answers is None:
        problems = ["the plan has no `## Premise checks` section"]
    else:
        problems = [
            f"answer {n} ({question}) is missing or empty"
            for n, question in enumerate(_QUESTIONS, start=1)
            if _empty(answers.get(n, ""))
        ]
    if not problems:
        return 0
    print(
        "BLOCKED (plan premise check): this plan names repo work, and\n"
        + "".join(f"  - {p}\n" for p in problems)
        + "\nAdd or complete this section in the live plan, then call ExitPlanMode again:\n"
        "\n  ## Premise checks\n"
        + "".join(f"  {n}. <{q}>\n" for n, q in enumerate(_QUESTIONS, start=1))
        + "\n`n/a — <reason>` is a valid answer. The questions:\n"
        '.claude/docs/premise-check.md, "At plan and issue time".',
        file=sys.stderr,
    )
    return 2


def main() -> int:
    try:
        payload = read_payload()
        if not payload:
            return 0
        return _check(payload)
    except Exception:  # noqa: BLE001 — fail open; a crash must not wedge planning
        return 0


if __name__ == "__main__":
    sys.exit(main())
