#!/usr/bin/env python3
"""Report the tests CI retried, and fail when a collected test reached no outcome.

WHY. CI retries a failed test once (pytest-rerunfailures, ``--force-reruns=1``), so a
test that fails and then passes no longer turns the run red (#5024). The
change's own test files are never retried (``--rerun-exclude-path``), so what is
left for this step is to make the retries visible and to catch the one way a
retry can hide a test outright: a collected test that ended with no outcome at
all. ``tests/_rerun_ledger.py`` says how that can happen and already fails the
pytest run for it; this step names the ids where the run summary shows them.

Every test that passed only on its retry is listed in the step summary. On main
pushes the ``flaky-report`` job files each one (``scripts/ci/file_flaky_issues.py``).

INPUT. The JSON ledger ``tests/_rerun_ledger.py`` writes.

EXIT. 0 clean (retried tests may be listed) / 1 a test with no outcome, or no
ledger after the tests ran / 2 refused (an unreadable or malformed ledger).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_KEYS = {
    "collected": int,
    "with_outcome": int,
    "missing": int,
    "missing_ids": list,
    "rerun_passed": list,
    "rerun_failed": list,
}


def load_ledger(path: str) -> dict | str:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="surrogateescape"))
    except FileNotFoundError:
        return "missing"
    except (OSError, ValueError) as exc:
        return f"unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return "malformed: not a JSON object"
    for key, kind in _KEYS.items():
        if not isinstance(data.get(key), kind):
            return f"malformed: {key!r} is not a {kind.__name__}"
    return data


def _md(text: object) -> str:
    # Ids come from test code, and this output reaches the step log, where a
    # line starting with "::" is a workflow command: escape every non-printable
    # character (a newline above all) so an id stays on its own line.
    safe = "".join(c if c.isprintable() else repr(c)[1:-1] for c in str(text))
    return safe.replace("|", "\\|").replace("`", "'")


def evaluate(ledger: dict) -> tuple[int, list[str]]:
    lines = ["### Retried tests", ""]
    if ledger["rerun_passed"]:
        lines += [f"{len(ledger['rerun_passed'])} test(s) passed only on their retry:", ""]
        lines += [f"- `{_md(n)}`" for n in ledger["rerun_passed"]]
    else:
        lines.append("No test passed only on its retry.")
    if ledger["rerun_failed"]:
        lines += [
            "",
            f"{len(ledger['rerun_failed'])} test(s) failed both attempts (named in the failure summary).",
        ]
    if not ledger["missing"]:
        return 0, lines
    lines += [
        "",
        f"**Failed:** {ledger['missing']} of {ledger['collected']} collected tests reached no outcome:",
        "",
    ]
    lines += [f"- `{_md(n)}`" for n in ledger["missing_ids"]]
    return 1, lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ledger")
    args = ap.parse_args(argv)
    ledger = load_ledger(args.ledger)
    if ledger == "missing":
        print(
            f"### Retried tests\n\nNo retry ledger at `{args.ledger}`: the tests ran but the "
            "retry accounting did not, so a test with no outcome cannot be ruled out."
        )
        return 1
    if isinstance(ledger, str):
        print(f"### Retried tests\n\nRefused: the retry ledger is {ledger}.")
        return 2
    code, lines = evaluate(ledger)
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())
