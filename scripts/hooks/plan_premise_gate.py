#!/usr/bin/env python3
"""A plan that specifies repo work answers the premise form before it is presented.

``.claude/docs/premise-check.md`` ("At plan and issue time") asks every plan that
specifies work to answer the premise form (six questions when this hook was
written; nine items since ``FORM_EPOCH``). Until this hook, nothing read the answers.
MEASURED 2026-10-27 over one install's 72 plans: 59 name a repo path, and 13 carry a
premise heading, under four spellings (grep, case-insensitive,
``^#{1,6} *premise (check|checks|questions)``).

PreToolUse: ExitPlanMode. It BLOCKS (stderr + exit 2, the adopt_first_gate contract),
every time, on two facts and nothing else:

  1. the live plan names a repo path but has no premise section (a heading titled
     "Premise check(s)" or "Premise question(s)", any level);
  2. one of the numbered answers is missing or empty (``n/a — <reason>`` is an
     answer; a bare ``n/a``, ``TBD`` or an unfilled ``<placeholder>`` is not).

TWO FORMS. The six questions, and the nine-item form (``FORM``): each answer
starts with its label, answer 2 carries ``Stated:`` and answer 5 ``Wrong fix:``,
and ``1. Problem: as #<N>`` stands for items 1 to 5. A section in the nine-item
form (told from its first answer) is judged by it. ``FORM_EPOCH`` is set, so a
plan owes the nine; a complete six is blocked with the nine-item template.

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
#: No answer at all: a placeholder, not a wrong answer. Matched on the answer's
#: RENDERED text (``premise_answers``), so ``**N/A**`` and `` `n/a` `` read as
#: ``N/A``. ``n/a`` alone (or with a dangling dash) is empty; ``n/a — <reason>`` is
#: an answer. The issue form's empty field, ``_No response_``, renders as
#: ``No response``.
_EMPTY = re.compile(
    r"^(?:_?no response_?"
    r"|(?:n/?a|n\.a\.|none|tbd|todo|not applicable)[.:]?(?:\s*[—–-]+)?"
    r"|\?+|[-—–\s]+|\.+|…)?$",
    re.IGNORECASE,
)
#: An unfilled template slot such as ``<caller named and tracked, or why there is
#: none>``, the block message's own remedy text. CommonMark drops a slot that parses
#: as an HTML tag, but one with commas or other non-attribute text stays literal, so
#: it is removed here rather than left to that accident.
_PLACEHOLDER = re.compile(r"<[^<>\n]*>")
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

#: The nine-item form: each answer starts with its label. Items 1 to 5 are the
#: Problem block, which every issue owes; 6 to 9 the Change block, which plans
#: and an issue handed to a paid builder owe as well. Kept beside the six above,
#: not in place of them: ``premise_gate`` counts issue completeness by these
#: tables, and the Action that runs it fetches only five files, so the form's
#: rules live here rather than in a module of their own.
FORM = (
    (
        "Observed",
        "what happens, with the command, output and commit (a fresh main), and the local causes ruled out",
    ),
    (
        "Intended",
        "what should happen. Stated: the doc, test, ruling or comment that says so, or nowhere",
    ),
    ("Cost", "who or what is affected, how often, how that was measured"),
    ("Searched", "existing issues, main and prior rulings: what was searched, what was found"),
    (
        "Acceptance",
        "what must hold afterwards. Wrong fix: the tempting fix that must fail, or none, because ...",
    ),
    ("Mechanism", "why this removes the cause; who calls it and what they do differently"),
    (
        "Alternatives",
        "what already does or decides this, the simpler mechanism considered, and why not each",
    ),
    ("Evidence", "every outside-behaviour claim and number, measured or cited"),
    ("Shape", "counted lines, and the split or the Shape: reason"),
)
FORM_QUESTIONS = tuple(f"{label}: {what}" for label, what in FORM)
PROBLEM_BLOCK = tuple(range(1, 6))
CHANGE_BLOCK = tuple(range(6, 10))
#: Answers that must carry a second label, and must answer it.
_SUBLABELS = {2: "Stated", 5: "Wrong fix"}
#: ``1. Problem: as #<N>`` stands for items 1 to 5: a plan or PR whose issue
#: already answers the Problem block cites it. The issue is not fetched (no
#: tracker lookup, as above).
_AS_LINE = re.compile(r"\s*problem\s*:\s*as\s+#\d+\b", re.IGNORECASE)
_NEW_FORM_FIRST = re.compile(r"\s*(?:observed\s*:|problem\s*:\s*as\s+#\d)", re.IGNORECASE)
#: When the nine-item form becomes REQUIRED. Plans and every paid build
#: (``devin-ready``) owe it from the moment this is set, whatever the date; the
#: date decides only which ISSUES owe it (created on or after it). None meant
#: not yet: a complete section in either form passed. Set by the change that
#: switched the issue forms and the docs, to a time after that change merged,
#: so an issue filed from the old forms before the merge is never labelled for
#: it. ``_TEST_PREMISE_FORM_EPOCH`` overrides it in tests that run the hook as a
#: subprocess, so no test depends on today's date
#: (``issue_premise_gate`` stamps a new issue with the wall clock). Write it as GitHub writes
#: ``created_at`` (``YYYY-MM-DDTHH:MM:SSZ``, like ``GATE_EPOCH``): issues compare
#: it as a string.
FORM_EPOCH: str | None = "2026-11-22T00:00:00Z"


def form_epoch() -> str | None:
    override = os.environ.get("_TEST_PREMISE_FORM_EPOCH")
    return override if override else FORM_EPOCH


#: Said when a section is owed in the nine-item form and is not written in it.
#: It does not claim the section IS the six: the form is told from answer 1, so a
#: nine-item section with a malformed first answer reads as "not nine-item" too.
NOT_NINE_NOTE = (
    "This section is not in the nine-item form: its first answer must start "
    "`Observed:` (or, in a plan or PR, `Problem: as #<N>`), and the other answers "
    "with their own labels (`Mechanism:` ... `Shape:`). If it is in the "
    "six-question form, the six map in: outside claims and numbers to 8 (Evidence), "
    "existing code and scope limits to 7 (Alternatives), the caller to 6 "
    "(Mechanism), the shape to 9 (Shape); items 1 to 5 state the problem."
)


def is_new_form(answers: dict[int, str] | None) -> bool:
    """Whether a premise section is written in the nine-item form.

    Its first answer starts ``Observed:`` or ``Problem: as #N``, AND another
    answer carries its own nine-item label (for the as-line, a Change-block
    answer). One label is not enough: old question 1 (outside-behaviour claims)
    is naturally answered "Observed: ...", and judging such a complete six by the
    nine-item form would newly fail it. Two coincidences in one old section are
    the residual this accepts."""
    if not answers or not _NEW_FORM_FIRST.match(answers.get(1, "")):
        return False
    others = CHANGE_BLOCK if _AS_LINE.match(answers.get(1, "")) else range(2, len(FORM) + 1)
    return any(_after_label(FORM[n - 1][0], answers.get(n, "")) is not None for n in others)


#: Every label the form uses, main and sub. A field that is only labels (``Cost:
#: Searched:`` read as item 3) is empty: a label is not an answer.
_ALL_LABELS = tuple(label for label, _ in FORM) + tuple(_SUBLABELS.values()) + ("Problem",)
_LEADING_LABELS = re.compile(
    r"\A\s*(?:(?:"
    + "|".join(r"\s+".join(re.escape(w) for w in lab.split()) for lab in _ALL_LABELS)
    + r")\s*:\s*)+",
    re.IGNORECASE,
)


def _sublabel_at(sub: str, body: str) -> re.Match[str] | None:
    """Where the sub-label ``sub:`` starts a field in ``body``, or None.

    A field starts at the beginning, after a sentence end or a newline, or where
    the label is written capitalized as the form writes it (``Stated:``,
    ``Wrong fix:``). Prose such as "works as stated: nothing" is not the field."""
    words = sub.split()
    loose = r"\s+".join(re.escape(w) for w in words)
    after_break = re.compile(rf"(?:\A|(?<=[.;!?)\n]))\s*({loose})\s*:", re.IGNORECASE)
    first, rest = words[0], words[1:]
    capital = re.escape(first[0].upper()) + "(?i:" + re.escape(first[1:]) + ")"
    tail = "".join(r"\s+(?i:" + re.escape(w) + ")" for w in rest)
    capitalized = re.compile(rf"\b({capital}{tail})\s*:")
    found = [m for m in (after_break.search(body), capitalized.search(body)) if m]
    return min(found, key=lambda m: m.start(1)) if found else None


def form_fields(n: int, answer: str) -> tuple[str, str | None] | None:
    """Answer ``n`` split into ``(main, sub)`` after its label, or None when the
    answer does not start with the label. ``sub`` is the text after the item's
    sub-label (None when it has none, or when the item takes none). Each part
    is judged on its own: a filled sub-label never fills an empty main answer."""
    body = _after_label(FORM[n - 1][0], answer)
    if body is None:
        return None
    sub = _SUBLABELS.get(n)
    # The template's own placeholder holds the sub-label's name ("<what should
    # happen. Stated: the doc ...>"), so search with placeholders blanked; the
    # offsets still index the original text.
    masked = _PLACEHOLDER.sub(lambda m: " " * len(m.group()), body)
    found = _sublabel_at(sub, masked) if sub else None
    if found is None:
        return body, None
    return body[: found.start(1)], body[found.end() :]


def six_missing(answers: dict[int, str]) -> list[int]:
    """Which of the six answers are empty, for the either-form rule.

    Today's rule, plus one case: in a section the nine-item form detector
    claims, an answer that is nothing but form labels and placeholders counts as
    empty. Otherwise the unfilled nine-item template, whose six first answers
    are bare labels, would pass as six answered questions. A label followed by
    any text ("Shape: none") keeps today's result, so an old six whose answers
    happen to open with a form word is unchanged. The residual: in such a
    section, an answer that is a bare label with no text ("Searched:") newly
    counts as empty."""
    if not is_new_form(answers):
        return [n for n in range(1, len(_QUESTIONS) + 1) if _empty(answers.get(n, ""))]
    return [
        n
        for n in range(1, len(_QUESTIONS) + 1)
        if _empty(answers.get(n, "")) or _label_only(answers.get(n, ""))
    ]


def _label_only(text: str) -> bool:
    """Whether ``text`` is nothing but a leading run of form labels and placeholders."""
    rest = _LEADING_LABELS.sub("", text, count=1)
    return not _PLACEHOLDER.sub("", rest).replace("\u200b", "").strip()


#: A proposal's answer 2: its ``Stated:`` field starts with the word the form
#: names, "nowhere", quotes and markup allowed (the form writes it ``"nowhere"``).
#: Owner ruling 2026-11-15: only that word; another wording that admits no
#: citation ("none — new behaviour") passes as cited, a stated residual. So does
#: the parser's rule for where a sub-label starts: a capitalized "as Stated:" in
#: the main answer opens the field early (``_sublabel_at``).
_NOWHERE = re.compile(r"[\W_]*nowhere\b", re.IGNORECASE)


def is_proposal(answers: dict[int, str] | None) -> bool:
    """Whether a nine-item section's answer 2 admits that nothing states the
    intended behaviour (``Stated: nowhere``): the issue is then a proposal, not a
    fix. Read from the ``Stated:`` field alone, so prose in the main answer never
    counts."""
    if not is_new_form(answers):
        return False
    fields = form_fields(2, answers.get(2, ""))
    if fields is None or fields[1] is None:
        return False
    return bool(_NOWHERE.match(_LEADING_LABELS.sub("", fields[1], count=1)))


def field_empty(text: str | None) -> bool:
    """Whether a field says nothing once a leading run of labels is taken off."""
    return text is None or _empty(_LEADING_LABELS.sub("", text, count=1))


def _after_label(label: str, answer: str) -> str | None:
    """The text after ``label:`` at the start of ``answer``, or None without it."""
    found = re.match(rf"\s*{re.escape(label)}\s*:\s*(.*)\Z", answer, re.IGNORECASE | re.DOTALL)
    return found.group(1) if found else None


def form_problems(
    answers: dict[int, str], need: tuple[int, ...], *, allow_as_line: bool = False
) -> list[tuple[int, str]]:
    """``(n, why)`` for each item in ``need`` that the nine-item form leaves
    unanswered. Structure only: whether an answer is TRUE is the reviewer's
    question (the owner ruling in the module docstring)."""
    # Plans and PR bodies only (spec 5.2): an issue answers its own Problem
    # block, and a paid build owes all nine written out.
    as_line = allow_as_line and bool(_AS_LINE.match(answers.get(1, "")))
    out: list[tuple[int, str]] = []
    for n in need:
        if as_line and n in PROBLEM_BLOCK:
            continue
        label = FORM[n - 1][0]
        answer = answers.get(n, "")
        fields = form_fields(n, answer)
        if fields is None:
            why = "is missing or empty" if _empty(answer) else f"does not start with `{label}:`"
            out.append((n, why))
            continue
        main, subtext = fields
        if field_empty(main):
            # An answer written inside the template's brackets is told so, not
            # only that it is empty (the six-question path's `_problem` reads
            # the whole answer; this reads the field after its label).
            rest = _LEADING_LABELS.sub("", main, count=1)
            rest = rest.replace("\u200b", "")
            only_placeholder = bool(_PLACEHOLDER.search(rest)) and not _PLACEHOLDER.sub("", rest).strip()
            why = (
                "is only a `<placeholder>`: replace it, brackets included"
                if only_placeholder
                else "is missing or empty"
            )
            out.append((n, why))
            continue
        sub = _SUBLABELS.get(n)
        if sub and field_empty(subtext):
            out.append((n, f"has no `{sub}:` answer"))
    return out


def _form_template() -> str:
    return "".join(f"  {n}. {label}: <{what}>\n" for n, (label, what) in enumerate(FORM, start=1))


def _old_template() -> str:
    return "".join(f"  {n}. <{q}>\n" for n, q in enumerate(_QUESTIONS, start=1))


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
    return bool(_EMPTY.match(_PLACEHOLDER.sub("", answer).replace("\u200b", "").strip()))


def _problem(n: int, question: str, answer: str) -> str:
    """Why answer ``n`` does not count. An answer written INSIDE the template's
    brackets reads as a placeholder; say so, rather than call written text empty."""
    if answer.strip() and not _EMPTY.match(answer.replace("\u200b", "").strip()):
        return f"answer {n} ({question}) is only a <placeholder>: replace it, brackets included"
    return f"answer {n} ({question}) is missing or empty"


def _check(payload: dict) -> int:
    prose = strip_quoted(live_section(_plan_text(payload)))
    if not _REPO_PATH.search(prose):
        return 0  # no repo work named — not a plan this check is for

    answers = premise_answers(prose)
    epoch = form_epoch()
    # The template shown is the form the plan owes: the nine-item form once it is
    # required, or when the plan is already written in it; the six otherwise.
    template = _form_template() if (epoch is not None or is_new_form(answers)) else _old_template()
    if answers is None:
        problems = ["the plan has no `## Premise checks` section"]
    elif epoch is None and not six_missing(answers):
        # Until the epoch, EITHER complete form passes: six answers pass here as
        # they do today (``six_missing``: only an answer that is nothing but form
        # labels newly counts as empty, so an unfilled template does not pass).
        problems = []
    elif is_new_form(answers):
        problems = [
            f"answer {n} ({FORM[n - 1][0]}) {why}"
            for n, why in form_problems(
                answers, PROBLEM_BLOCK + CHANGE_BLOCK, allow_as_line=True
            )
        ]
    elif epoch is not None:
        problems = [NOT_NINE_NOTE]
    else:
        problems = [
            _problem(n, question, answers.get(n, ""))
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
        + template
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
