#!/usr/bin/env python3
"""File each test that passed only on its retry on main as a ``flaky`` issue (#5024).

The ``flaky-report`` CI job runs this after a main push whose ``test`` job retried
a test (the ledger ``tests/_rerun_ledger.py`` writes). The first occurrence on
main is enough: a pull request's own run is not durable, and the title is the
dedup key, so a test keeps exactly one open issue however often it flakes.

For each retried node id:

- the title is ``Flaky test: <node id>``. GitHub caps titles at 256 characters,
  so a longer id is titled by its file and a short hash of the full id, and the
  full id is in the body (``<id omitted from title: full id in body>``);
- an open issue labelled ``flaky`` with that exact title gets a comment naming
  the run and the commit;
- otherwise a new issue is opened with the label.

Node ids come from a CI artifact, so they are untrusted text: each must match a
node-id grammar (a ``tests/`` path, ``::``, printable, at most 512 characters) or
it is skipped with a warning, and every ``gh`` call is an argv list, never a
shell string.

``--dry-run`` prints the intended writes and makes none (the job's
``workflow_dispatch`` path). The ``FLAKY_AUTOFILE`` repository variable, read by
the workflow, switches the job off.

EXIT. 0 done / 1 at least one id could not be filed / 2 refused (bad ledger, or
the open-issue listing was unreadable or hit its limit).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path

LABEL = "flaky"
TITLE_PREFIX = "Flaky test: "
TITLE_MAX = 256  # GitHub's issue title limit
ID_MAX = 512
LIST_LIMIT = 1000
# One broken shared fixture can make hundreds of tests pass only on retry in a
# single run; that is one defect, not hundreds of issues. Past this many issue
# writes in a run the rest are named in the log and left for the next run (the
# step summary of the test job lists every one).
MAX_WRITES = 20
# Printable, no backtick (the body wraps the id in a code span), a tests/ path.
_ID_RE = re.compile(r"tests/[^\s:`][^\x00-\x1f\x7f`]*::[^\x00-\x1f\x7f`]+")

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), capture_output=True, text=True, timeout=120)


def valid_id(nodeid: object) -> bool:
    # Format and separator characters (bidi overrides, zero-width joiners, line
    # and paragraph separators) could disguise a title, so they are refused too.
    return (
        isinstance(nodeid, str)
        and len(nodeid) <= ID_MAX
        and bool(_ID_RE.fullmatch(nodeid))
        and not any(unicodedata.category(c) in ("Cf", "Zl", "Zp") for c in nodeid)
    )


def title_for(nodeid: str) -> str:
    title = TITLE_PREFIX + nodeid
    if len(title) <= TITLE_MAX:
        return title
    digest = hashlib.sha256(nodeid.encode("utf-8", "surrogateescape")).hexdigest()[:12]
    stem = f"{TITLE_PREFIX}{nodeid.split('::', 1)[0]}:: <id omitted from title: full id in body> #{digest}"
    if len(stem) > TITLE_MAX:  # the file path alone is too long: the digest is the whole key
        stem = f"{TITLE_PREFIX}<id omitted from title: full id in body> #{digest}"
    return stem


def ensure_label(repo: str, run: Runner, dry_run: bool) -> str | None:
    """None when the label exists (or was just created), else why it cannot be used.

    ``gh issue create --label`` refuses a label the repository does not have, so
    without this every create fails. An existing label is never rewritten (no
    ``--force``): its colour and description are the owner's."""
    if run(["gh", "api", f"repos/{repo}/labels/{LABEL}", "--silent"]).returncode == 0:
        return None
    if dry_run:
        print(f"dry run: would create the {LABEL!r} label")
        return None
    out = run(
        [
            "gh",
            "label",
            "create",
            LABEL,
            "--repo",
            repo,
            "--color",
            "d93f0b",
            "--description",
            "A test that passed only on its CI retry",
        ]
    )
    if out.returncode != 0:
        return (
            f"the {LABEL!r} label is missing and could not be created: {out.stderr.strip()[:300]}"
        )
    print(f"created the {LABEL!r} label")
    return None


def open_flaky_titles(repo: str, run: Runner) -> dict[str, int] | str:
    out = run(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            repo,
            "--label",
            LABEL,
            "--state",
            "open",
            "--limit",
            str(LIST_LIMIT),
            "--json",
            "number,title",
        ]
    )
    if out.returncode != 0:
        return f"listing open {LABEL} issues failed: {out.stderr.strip()[:300]}"
    try:
        rows = json.loads(out.stdout)
    except ValueError as exc:
        return f"listing open {LABEL} issues returned unreadable JSON: {exc}"
    if len(rows) >= LIST_LIMIT:
        return f"{len(rows)} open {LABEL} issues reached the listing limit of {LIST_LIMIT}; dedup would be unsound"
    return {row["title"]: row["number"] for row in rows}


def main(argv: list[str] | None = None, run: Runner = _run) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("ledger")
    ap.add_argument("--repo", required=True)
    ap.add_argument("--run-url", required=True)
    ap.add_argument("--sha", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)

    try:
        ledger = json.loads(Path(args.ledger).read_text(encoding="utf-8", errors="surrogateescape"))
        ids = ledger["rerun_passed"]
        if not isinstance(ids, list):
            raise TypeError("rerun_passed is not a list")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"refused: the retry ledger is unusable ({type(exc).__name__}: {exc})")
        return 2
    if not ids:
        print("no test passed only on its retry; nothing to file")
        return 0
    label_problem = ensure_label(args.repo, run, args.dry_run)
    if label_problem:
        print(f"refused: {label_problem}")
        return 2
    existing = open_flaky_titles(args.repo, run)
    if isinstance(existing, str):
        print(f"refused: {existing}")
        return 2

    failed = 0
    valid = []
    for nodeid in ids:
        if valid_id(nodeid):
            valid.append(nodeid)
        else:
            print(
                f"::warning::skipped a retried id that is not a test node id (first 120 chars): {str(nodeid)[:120]!r}"
            )
    if len(valid) > MAX_WRITES:
        print(
            f"::warning::{len(valid)} tests passed only on retry; filing the first {MAX_WRITES}, "
            f"{len(valid) - MAX_WRITES} not filed this run (listed in the test job's summary)"
        )
        for nodeid in valid[MAX_WRITES:]:
            print(f"not filed this run: {nodeid!r}")
    for nodeid in valid[:MAX_WRITES]:
        title = title_for(nodeid)
        note = f"Passed only on its retry in {args.run_url} at commit {args.sha}."
        if title in existing:
            argv = [
                "gh",
                "issue",
                "comment",
                str(existing[title]),
                "--repo",
                args.repo,
                "--body",
                note,
            ]
            what = f"comment on #{existing[title]}"
        else:
            body = (
                f"`{nodeid}` failed and then passed on its automatic retry in a main CI run.\n\n"
                f"{note}\n\nOpened by the CI `flaky-report` job; later occurrences are added as comments."
            )
            argv = [
                "gh",
                "issue",
                "create",
                "--repo",
                args.repo,
                "--title",
                title,
                "--label",
                LABEL,
                "--body",
                body,
            ]
            what = f"open {title!r}"
        if args.dry_run:
            print(f"dry run: would {what}")
            continue
        out = run(argv)
        if out.returncode != 0:
            failed += 1
            print(f"::warning::could not {what}: {out.stderr.strip()[:300]}")
        else:
            print(f"did {what}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
