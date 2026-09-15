#!/usr/bin/env python3
"""Name every failed test somewhere log truncation cannot reach.

WHY. The CI ``test`` job runs ~26,000 tests, and pytest prints its FAILURES
block at the TAIL of the step's output — which is exactly the part GitHub's
log APIs drop when a step's log exceeds their cap. MEASURED 2026-09-14 on run
34898229755: ``--log``, ``--log-failed``, the whole-run log and the raw
endpoint all cut the step at ~44% of the suite, so a red ``test`` job named
NO failing test through any route GitHub offers, and the junit report that
held the names was discarded with the runner. A failure nobody can read is
diagnosed by guesswork, and guesswork was what this cost on the day it was
measured.

WHAT IT DOES. Reads the junit report pytest already writes and prints a short
Markdown summary — the failed/errored test ids, each with the first line of
its message — for ``$GITHUB_STEP_SUMMARY``, which is rendered by the Actions
UI outside the log entirely. The full report travels separately as an
artifact; this is the part a human reads first.

SELECTION, NOT AMPUTATION. Every id that is printed is printed WHOLE, and the
per-failure MESSAGE is selected down to its first line because the complete
text lives in the artifact and the first line of a pytest failure names the
assertion. Nothing is cut mid-value.

There IS one bound, and it is an external budget rather than a self-imposed
one: GitHub caps a step summary at 1 MiB, and a summary that exceeds it is
not rendered — so an unbounded list does not print 5,000 failures, it prints
NOTHING and takes the artifact pointer down with it. The budget is therefore
spent on WHOLE rows, the closing pointer is RESERVED before any row is
written so it cannot be the thing that falls off, and if the budget runs out
the count omitted is stated explicitly with its denominator. A reader is
never left to infer completeness: either every row is here, or a line says
how many are not and where all of them live.

REFUSAL. "Could not read the report" and "read it, and nothing failed" must
never look alike. A missing or unparseable report prints a visible notice and
exits 0 — this runs only when the job is ALREADY red, so its own exit code
must never replace the real failure with a secondary one; the notice, not the
code, carries the fact. (pytest dying before writing any report — a crash in
collection, an OOM kill — is exactly the case the notice names.)

Exit: always 0. The job is already failing; this only narrates.
"""

from __future__ import annotations

import sys
from pathlib import Path
from xml.etree import ElementTree

_NOTICE = "ci-failure-summary:"

#: GitHub renders nothing at all when a step summary exceeds 1 MiB, so this is
#: a hard external ceiling, not a preference. Bytes, because the cap is on the
#: file and a test id can carry multibyte characters.
_SUMMARY_BUDGET_BYTES = 1024 * 1024

#: Reserved up front for the "N of M not listed" line, so the declaration that
#: the list was bounded can never itself be the row that does not fit. A
#: bounded list that cannot say it was bounded reads as a complete one.
_OMISSION_RESERVE = 400


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"{_NOTICE} usage: ci_failure_summary.py <junit.xml>")
        return 0
    report = Path(argv[1])
    if not report.is_file():
        print(
            f"{_NOTICE} no report at {report} — pytest died before writing one "
            "(collection crash, runner kill). The failure is in the step log "
            "BEFORE the test output starts, which truncation does not reach."
        )
        return 0
    try:
        # S314 justification: the document is pytest's OWN --junit-xml output,
        # written moments earlier by the same job on the same runner — not
        # network input. Same disposition as check_skip_ceiling.py, which
        # reads the identical file.
        root = ElementTree.parse(report).getroot()  # noqa: S314
    except ElementTree.ParseError as exc:
        print(f"{_NOTICE} unparseable report at {report}: {exc}")
        return 0

    rows: list[tuple[str, str, str]] = []  # (kind, test id, first message line)
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
            message = _first_line(node.get("message")) or _first_line(node.text)
            rows.append((kind.upper(), test_id, message))

    if not rows:
        print(
            f"{_NOTICE} the report records no failed or errored testcase. The "
            "red step failed OUTSIDE pytest's own accounting — an exit-code "
            "path such as a segfault mid-run, or pytest's own internal error; "
            "the tail of the step log (not the head) holds the cause."
        )
        return 0

    header = f"## {len(rows)} failed/errored test(s)\n"
    trailer = "\nFull tracebacks: the `junit-test-report` artifact on this run."

    lines: list[str] = []
    spent = len(header.encode()) + len(trailer.encode()) + _OMISSION_RESERVE
    for index, (kind, test_id, message) in enumerate(rows):
        line = f"- **{kind}** `{test_id}`"
        if message:
            line += f" — {message}"
        cost = len(line.encode()) + 1
        if spent + cost > _SUMMARY_BUDGET_BYTES:
            omitted = len(rows) - index
            lines.append(
                f"\n> **{omitted} of {len(rows)} not listed here** — the step "
                "summary hit GitHub's 1 MiB cap, and a summary over that cap is "
                "not rendered at all. Every one of the "
                f"{len(rows)} is in the artifact below."
            )
            break
        spent += cost
        lines.append(line)

    print(header)
    for line in lines:
        print(line)
    print(trailer)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
