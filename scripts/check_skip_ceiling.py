#!/usr/bin/env python3
"""Watch the suite's SKIP count for an unexplained rise.

WHY. A test that stops running is invisible. It does not turn the suite red;
it turns it *smaller*, and a smaller green suite reads exactly like a healthy
one. CI has no assertion on collection or skip counts today.

TWO AXES, because dormancy moves in both directions and one number cannot see
both. A broken ``skipif`` RAISES the skip count — the ceiling catches that. A
marker lost in a refactor, or a directory that falls out of collection, LOWERS
or leaves it unchanged, so the ceiling is structurally blind to them; the
COLLECTED count is the only instrument for those. MEASURED on a 4-test sandbox
at ``--ceiling 1``: dropping half the suite printed "1 skipped of 2 collected.
OK." Each failure message names only the causes ITS axis can produce — a
ceiling trip is never a lost directory, and a floor trip is never an xfail.

WHAT IT DOES NOT DO. It is a ceiling, never a floor. Removing a skip means a
dormant test came back; a checker that punished that would be arguing for
dormancy. Only an increase past the recorded ceiling fails.

WHY IT SHIPS WITHOUT A CEILING. The honest baseline is a number measured on the
CI runners, and inventing one is how a guard gets a threshold nobody trusts and
everybody works around. With no ``--ceiling`` this REPORTS the count and exits
0, so the first real CI run produces the baseline. Setting the ceiling is then a
one-line follow-up against a measured number.

REFUSAL. "Could not measure" and "measured, and it is fine" must never share an
exit code. A missing report, an unparseable one, a run that collected zero
tests, or a nonsense ceiling all exit 2 — never 0. Every refusal names itself in
the output, because the interpreter ALSO exits 2 when it cannot open this file,
and a caller keying on the code alone could not tell the two apart.

NOTE. pytest's junit writer folds xfail into ``skipped``, so this is a
skip+xfail ceiling: marking a known-broken test xfail raises the number.
That is not wrong to count — an xfail is also a test not really running —
but it is the kind of silent conflation that earns a threshold nobody
trusts, so it is stated here and in the failure text.

Exit: 0 ok / 1 ceiling exceeded / 2 refused to measure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from xml.etree import ElementTree

# Named in every line so a refusal is distinguishable from the interpreter's own
# exit 2 on a missing script. The test suite asserts on this string.
TAG = "SKIP-CEILING"

OK, EXCEEDED, REFUSED = 0, 1, 2


def _say(msg: str) -> None:
    print(f"{TAG}: {msg}")


def _refuse(msg: str) -> int:
    print(f"{TAG}: REFUSING — {msg}", file=sys.stderr)
    return REFUSED


def _parse_bound(raw: str | None, flag: str, noun: str) -> int | None | str:
    """Return the bound, None for report mode, or a str describing why not.

    Parametrised rather than string-patched: an earlier version derived the
    floor's message by ``.replace("ceiling", "min-collected")`` over the
    ceiling's, which rewrote the operator's OWN echoed input (``--min-collected
    my-ceiling-value`` came back as ``'my-min-collected-value'``) and blamed the
    skip axis for a collected-count error.
    """
    if raw is None:
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return f"{flag} {raw!r} is not an integer"
    if value < 0:
        return f"{flag} {value} is negative; a {noun} cannot go below zero"
    return value


def _read_counts(path: Path) -> tuple[int, int] | str:
    """Return (collected, skipped, dirty) summed over every testsuite, or a reason.

    ``dirty`` is errors+failures. The step runs under ``!cancelled()``, so it
    also runs on FAILED runs — where the counts describe a corpus that did not
    finish, and a baseline read off it would be wrong in the one place the
    docstring promises honesty.
    """
    try:
        # S314 justification: the document is pytest's OWN --junit-xml
        # output, written by the same CI job seconds earlier. There is no
        # untrusted-author path — anyone who can write this file already has
        # write access inside the runner, at which point the XML parser is not
        # the weak link. defusedxml is deliberately NOT used: it is not a
        # declared dependency (checked pyproject.toml), so importing it here
        # would work locally and ImportError in CI.
        tree = ElementTree.parse(path)  # noqa: S314
    except FileNotFoundError:
        return f"no report at {path} — the test step did not produce one"
    except (ElementTree.ParseError, OSError) as exc:
        return f"report at {path} is unreadable: {type(exc).__name__}: {exc}"

    root = tree.getroot()
    suites = list(root.iter("testsuite"))
    if not suites:
        return f"report at {path} contains no <testsuite> element"

    if any(suite.find("testsuite") is not None for suite in suites):
        return f"report at {path} nests <testsuite>; counts would double"

    collected = skipped = dirty = 0
    for suite in suites:
        raw_tests = suite.get("tests")
        raw_skipped = suite.get("skipped")
        # A suite that does not state both counts cannot be summed. Guessing
        # zero here is exactly the silent under-read this script exists to stop.
        if raw_tests is None or raw_skipped is None:
            name = suite.get("name", "<unnamed>")
            return f"testsuite {name!r} omits {'tests' if raw_tests is None else 'skipped'}"
        try:
            n_tests, n_skipped = int(raw_tests), int(raw_skipped)
        except ValueError:
            return f"testsuite {suite.get('name', '<unnamed>')!r} has non-numeric counts"
        # The same invariant refused for --ceiling, applied to the report too:
        # an operator's negative count is refused, so the file's must be as well.
        if n_tests < 0 or n_skipped < 0:
            return f"testsuite {suite.get('name', '<unnamed>')!r} reports negative counts"
        collected += n_tests
        skipped += n_skipped
        # Absent errors/failures attributes are treated as zero, not refused:
        # they are optional in the junit schema and their absence says nothing
        # about whether the run was clean.
        for attr in ("errors", "failures"):
            try:
                dirty += int(suite.get(attr) or 0)
            except ValueError:
                return (
                    f"testsuite {suite.get('name', '<unnamed>')!r} has a "
                    f"non-numeric {attr} count"
                )
    return collected, skipped, dirty


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", help="pytest --junit-xml report path")
    parser.add_argument(
        "--ceiling",
        default=None,
        help="max skips allowed; omit to REPORT the count without enforcing",
    )
    parser.add_argument(
        "--min-collected",
        default=None,
        help="min tests that must be collected; omit to REPORT without enforcing",
    )
    args = parser.parse_args(argv)

    ceiling = _parse_bound(args.ceiling, "ceiling", "skip count")
    if isinstance(ceiling, str):
        return _refuse(ceiling)

    floor = _parse_bound(args.min_collected, "min-collected", "collected count")
    if isinstance(floor, str):
        return _refuse(floor)

    counts = _read_counts(Path(args.report))
    if isinstance(counts, str):
        return _refuse(counts)
    collected, skipped, dirty = counts

    # The empty-corpus trap: zero collected tests yields zero skips, which is
    # indistinguishable from a healthy suite unless it is refused outright.
    if collected == 0:
        return _refuse("the report collected 0 tests, so its 0 skips measure nothing")

    # The floor is checked FIRST: if tests vanished from collection, the skip
    # count is measured over a corpus that is already wrong, so its verdict is
    # not the useful one to report.
    if floor is not None and collected < floor:
        print(
            f"{TAG}: FAIL — {collected} tests collected, floor is {floor}. "
            f"{floor - collected} test(s) stopped being collected at all: a "
            f"marker lost in a refactor, a directory out of collection, or a "
            f"collection error. If the removal was deliberate, lower the floor "
            f"in the same commit.",
            file=sys.stderr,
        )
        return EXCEEDED

    if ceiling is None and floor is None:
        if dirty:
            _say(
                f"{skipped} skipped of {collected} collected. REPORT ONLY — not "
                f"enforcing. NO baseline offered: this run had {dirty} error(s)/"
                f"failure(s), so its corpus is not the healthy one to lock in. "
                f"Read this line again on a green run."
            )
            return OK
        _say(
            f"{skipped} skipped of {collected} collected. "
            f"REPORT ONLY — not enforcing (no --ceiling / --min-collected given). "
            f"Set --ceiling {skipped} --min-collected {collected} to lock this in."
        )
        return OK

    if ceiling is None:
        _say(
            f"{skipped} skipped of {collected} collected, floor {floor}. "
            f"OK (skip ceiling not enforced; set --ceiling {skipped})."
        )
        return OK

    if skipped > ceiling:
        print(
            f"{TAG}: FAIL — {skipped} skipped, ceiling is {ceiling}. "
            f"{skipped - ceiling} more test(s) are being skipped than expected. "
            f"Either a skipif condition broke and is now always true, or a skip "
            f"or xfail was added deliberately (junit counts xfail as skipped) "
            f"and the ceiling needs raising in the same commit. NOT a lost "
            f"marker or a dropped directory — those LOWER this number; "
            f"--min-collected is the check for them.",
            file=sys.stderr,
        )
        return EXCEEDED

    if floor is None:
        # The likely misconfiguration: this script and its CI step are both
        # named for the ceiling, so an operator pasting one flag pastes this
        # one — silently restoring the gap the floor was added to close.
        _say(
            f"{skipped} skipped of {collected} collected, ceiling {ceiling}. "
            f"OK (collection floor NOT enforced — the ceiling is blind to a lost "
            f"marker or a dropped directory; set --min-collected {collected})."
        )
        return OK

    _say(
        f"{skipped} skipped of {collected} collected, ceiling {ceiling}, "
        f"floor {floor}. OK."
    )
    return OK


if __name__ == "__main__":
    sys.exit(main())
