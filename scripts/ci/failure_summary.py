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

#: A ceiling on the breadcrumb, and a RESOURCE guard rather than a preference:
#: the crash notice shares GitHub's 1 MiB step-summary cap, and a summary over
#: that cap renders as NOTHING at all — so an unbounded id is a way to lose the
#: whole summary, not merely a way to make it long.
#:
#: MEASURED 2026-09-16 over 26,798 node ids — a LOWER BOUND, not a census: that
#: is a local collect-only run with CI's ignore set, and CI itself collected
#: 27,218 at this commit, so ~400 ids were never sampled. Every figure below is
#: therefore "at least". p50 91, p99 173, p99.9 215; FOUR ids cross this
#: ceiling (200,105 / 110,106 / 20,105 / 6,993), and the top three are
#: parametrised cases whose PARAMETER is a large string — a payload, not a
#: name. 4096 sits far above every id that actually names a test (the
#: fifth-longest is under 500) and bounds the notice at 4 KiB of the budget.
#:
#: The previous bound was 200, which silently cut at least 54 ids (0.2%) across
#: 18 test files — a truncated id reads as a whole one, so a crash in a long
#: parametrised case was reported under a name that identified the wrong case.
#: When this one bites it SAYS so and states the true length.
_ACTIVE_TEST_CEILING = 4096


def _fence(text: str) -> str:
    """Render arbitrary test-controlled text so it cannot escape its own row.

    Test ids and exception messages are whatever a test happens to be named or
    to raise, and this repository parametrises over shell payloads, backticks
    and HTML-like strings by the hundred. Interpolated raw into Markdown, a
    message ending in an unclosed ``<!--`` comments out every row BELOW it and
    the artifact pointer with it — the summary then reads as though those
    failures did not exist, which is precisely the silent under-read this whole
    script was written to stop.

    Backslash-escaping is not enough on its own because an unbalanced backtick
    also breaks the code span, so the row is rendered inside a fence sized to
    exceed the longest backtick run the value itself contains.
    """
    flat = text.replace("\r", " ").replace("\n", " ")
    longest = 0
    run = 0
    for ch in flat:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    fence = "`" * (longest + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{fence}{pad}{flat}{pad}{fence}"


def _first_line(text: str | None) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _active_test() -> str:
    """The node id of the last test to START, or "" if unavailable.

    Written by the conftest hook before each test and fsynced, so it survives a
    process death that never reaches pytest's own reporting. Best-effort by
    construction: every failure to read it returns "" and the caller simply says
    less, because a narrator that raises while explaining a failure is worse
    than one that is vague.

    The id comes back WHOLE unless it crosses ``_ACTIVE_TEST_CEILING``, in which
    case the return value says so and states the true length.
    """
    import os

    path = os.environ.get("GENESIS_ACTIVE_TEST_FILE", "")
    if not path:
        return ""
    try:
        name = Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    if not name:
        return ""
    first = name.splitlines()[0]
    if len(first) <= _ACTIVE_TEST_CEILING:
        return first
    # DECLARED, never silent. The whole point of this value is to identify one
    # case, and a cut suffix is exactly the part that distinguishes a
    # parametrised case from its siblings — so a reader who is handed a short
    # id must be able to tell it apart from a complete one.
    return (
        f"{first[:_ACTIVE_TEST_CEILING]}… [TRUNCATED — the node id is "
        f"{len(first)} characters, so this names the parametrised case only "
        "up to its first 4096]"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"{_NOTICE} usage: failure_summary.py <junit.xml>")
        return 0
    report = Path(argv[1])
    if not report.is_file():
        # A HARD CRASH inside a running test -- a segfault, an OOM kill,
        # `os._exit` -- never writes the report, and dropping `-v` means the log
        # no longer names the test that was running either. So the name comes
        # from a channel that survives the crash: the active-test breadcrumb the
        # conftest rewrites before each test and fsyncs. Without it this notice
        # can only say "something died", which is exactly the diagnosability the
        # verbosity used to buy.
        active = _active_test()
        where = (
            f" The last test to START was {_fence(active)}, so the crash is at or "
            "immediately after it."
            if active
            else ""
        )
        # The two causes need DIFFERENT directions, and conflating them sends
        # the reader to the wrong end of the log. A collection or startup crash
        # dies before any test runs, so its output really is at the HEAD, which
        # truncation does not reach. A hard crash INSIDE a test dies wherever
        # the run had got to -- which on a long run is the truncated tail, the
        # exact region this whole change exists because nobody can read. There
        # the breadcrumb IS the evidence, and saying "look at the head" would be
        # advice that cannot be followed.
        where_to_look = (
            "The crash is inside the run, so its output is wherever the run had "
            "reached — on a long run that is the truncated tail, and the name "
            "above is the evidence that survives it."
            if active
            else "No test had started, so this is a collection or startup "
            "failure: its output is at the HEAD of the step log, which "
            "truncation does not reach."
        )
        print(
            f"{_NOTICE} no report at {report} — pytest died before writing one "
            "(collection crash, runner kill, or a hard crash inside a test)."
            f"{where} {where_to_look}"
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
    except OSError as exc:
        # A SEPARATE handler, and a separate word, because these are different
        # facts: unparseable means pytest wrote a damaged document, unreadable
        # means we could not get at an intact one (a permission fault, a runner
        # filesystem error). `parse` raises OSError for the second, which a
        # handler catching only ParseError let escape -- and this narrator runs
        # in a step that fires BECAUSE something already failed, so exiting
        # non-zero here adds a second red herring to the one it exists to
        # explain. `check_skip_ceiling.py` already catches both on this file.
        print(f"{_NOTICE} unreadable report at {report}: {exc}")
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
    omitted = 0
    for kind, test_id, message in rows:
        # The ID and the MESSAGE are budgeted SEPARATELY, because they are not
        # equally important and they are not equally bounded. The id is the
        # answer to "which test failed" and is short; the message is a nicety
        # and is whatever the test raised. A single
        # `RuntimeError("x" * 2_000_000)` used to make its row exceed the whole
        # budget, and the loop then BROKE -- so one pathological first failure
        # produced a summary naming ZERO tests, with every later row that would
        # have fitted discarded behind it.
        id_line = f"- **{kind}** {_fence(test_id)}"
        id_cost = len(id_line.encode()) + 1
        if spent + id_cost > _SUMMARY_BUDGET_BYTES:
            # Only an ID that cannot fit counts as omitted, and we keep going:
            # a later row may still fit, and the count must be of rows actually
            # dropped rather than of everything after the first big one.
            omitted += 1
            continue
        line = id_line
        spent += id_cost
        if message:
            addition = f" — {_fence(message)}"
            add_cost = len(addition.encode())
            if spent + add_cost <= _SUMMARY_BUDGET_BYTES:
                line += addition
                spent += add_cost
            else:
                # The id survives without its message. Say so, rather than
                # letting a bare row read as a test that failed silently.
                line += " — _(message omitted: it would not fit the summary cap;"
                line += " it is in the artifact below)_"
                spent += 96
        lines.append(line)
    if omitted:
        lines.append(
            f"\n> **{omitted} of {len(rows)} not listed here** — the step "
            "summary hit GitHub's 1 MiB cap, and a summary over that cap is "
            "not rendered at all. Every one of the "
            f"{len(rows)} is in the artifact below."
        )

    print(header)
    for line in lines:
        print(line)
    print(trailer)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
