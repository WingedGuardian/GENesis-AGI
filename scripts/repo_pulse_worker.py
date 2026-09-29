#!/usr/bin/env python3
"""Entry point for the detached repo-pulse worker (session-manager PR-4a).

Spawned by the SessionStart hook at startup/resume/compact boundaries:

    python scripts/repo_pulse_worker.py --trigger session_start \
        [--db-path <genesis.db>]

Manual / E2E form (bypasses the 30-minute global debounce):

    python scripts/repo_pulse_worker.py --trigger manual --force \
        [--lookback-days 7]

Exit code is always 0 unless argument parsing fails — outcomes (including
errors) are recorded in repo_pulse_runs and the call-site telemetry row,
because nothing is attached to read a detached process's exit status.
Uncaught early failures land on stderr, which the hook redirects to
~/.genesis/session_awareness/repo_pulse_err.log.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _print_verification_backlog(db_path: str | None) -> None:
    """The pr_verifications day-one reader: open obligations, oldest merge first.

    Read-only, no worker run, no debounce — usable while the Wave-3 validator
    session (the eventual consumer) does not exist yet. Prints one line per
    open row plus a status histogram; "0 open" with a nonzero closed count
    means the lane is running and everything recent was docs-exempt or
    verified, while an EMPTY histogram means the table is empty or
    pre-migration — two different states, both printed as what they are.
    """
    import asyncio as _asyncio

    from genesis.db.crud import pr_verifications as verif_crud
    from genesis.env import genesis_db_path

    resolved = db_path or str(genesis_db_path())

    if not Path(resolved).exists():
        # mode=ro below refuses to create the file, which is the point — but a
        # bare failure would read as a defect rather than "not set up yet".
        print(f"pr_verifications: no database at {resolved}")
        return

    # Admission fence (fail-closed): a quarantined
    # database is not opened even read-only — this worker runs automatically
    # at session boundaries, exactly the entry class the fence exists to bind.
    try:
        from genesis.db.admission import database_is_fenced

        fenced = database_is_fenced(resolved)
    except Exception:
        fenced = True
    if fenced:
        print("pr_verifications: database quarantined — skipped")
        return

    async def _read() -> tuple[list[dict], dict]:
        import aiosqlite

        # mode=ro, not a plain path: this command only REPORTS. A read-write
        # handle would create an empty database when pointed at a wrong path and
        # then truthfully report it as empty. `mode=ro` (not `immutable=1`) is
        # the WAL-aware read-only form — `immutable` ignores the -wal file and
        # would miss rows the worker committed moments earlier.
        #
        # The URI is BUILT, never interpolated. `?` and `#` are legal POSIX
        # filename characters with URI meaning, so an f-string let SQLite parse
        # part of a real path as a query or fragment: it opened a different,
        # shorter path — and could swallow `mode=ro` itself, turning the
        # report-only guarantee above into a read-write handle that CREATES
        # that unintended file, while the existence check above had validated
        # the original path and said nothing (Codex P2, PR #1836).
        uri = f"{Path(resolved).absolute().as_uri()}?mode=ro"
        async with aiosqlite.connect(uri, uri=True, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            return await verif_crud.list_open(db), await verif_crud.counts(db)

    rows, histogram = _asyncio.run(_read())
    if not histogram:
        print("pr_verifications: no rows (table empty or pre-migration)")
        return
    for row in rows:
        title = (row.get("pr_title") or "").strip()
        # The row's identity is (repo, pr_number), so printing the number alone
        # is a partial key: two repos — a fork, or a rename — can both hold a
        # PR #12, and the reader could neither tell which row was pending nor
        # supply the `repo` argument `close_verification` requires without
        # going to SQLite (Codex P2, PR #1836).
        print(
            f"OPEN  {row.get('repo') or '<unknown repo>'}#{row['pr_number']}  "
            f"merged {str(row['merged_at'])[:10]}  {title[:80]}"
        )
        # A PARKED row: a validator attempted it and could not discharge it. Shown
        # inline because the alternative is a validator re-deriving a conclusion
        # someone already reached — which is the whole reason the note is stored.
        # `verdict` is only ever set on an OPEN row by a non-closing attempt (a
        # pass closes the row), so its presence IS the parked signal.
        if row.get("verdict"):
            note = (row.get("last_attempt_note") or "").strip()
            tries = row.get("attempt_count") or 0
            print(
                f"      ATTEMPTED {tries}x  {row['verdict']}{'  — ' + note[:120] if note else ''}"
            )
    # `list_open` has its own row cap, so the lines above can be a SUBSET while
    # the histogram below reports the true total — printing both without saying
    # so lets the two numbers disagree in silence, and a reader who counts the
    # lines gets a wrong answer that looks complete. The omission is stated with
    # both numbers, which are known exactly here.
    open_total = histogram.get("open", 0)
    if len(rows) < open_total:
        print(
            f"  <listed the {len(rows)} oldest of {open_total} open row(s); "
            f"{open_total - len(rows)} not shown — close some, or query "
            f"pr_verifications directly for the full set>"
        )
    print(f"pr_verifications: {open_total} open, {histogram.get('closed', 0)} closed ({resolved})")
    print(
        "  close one: python3 scripts/pr_verification.py close --pr <N> --verdict "
        "<pass-mechanical|pass-with-measured-gaps|fail-intent|cannot-verify> "
        "--evidence-file <doc.json>   "
        "(--verification-log --pr <N> shows what was decided, on any row)"
    )


#: Characters of one evidence document a scoped read prints before clipping. The
#: document is capped at 256 KiB by the writer, which is far more than belongs in a
#: terminal; this is the reading budget, and a clip SAYS it clipped.
_EVIDENCE_SHOW = 8000

#: Rows one `--verification-log` run will show. Named rather than inline so the
#: truncation disclosure below cannot drift from the value it describes.
_LOG_LIMIT = 500


def _print_verification_log(db_path: str | None, pr_number: int | None) -> None:
    """What a validator DECIDED about an obligation, and on what evidence.

    UNSCOPED it lists discharged rows — the census of what has been closed. SCOPED to a
    PR it shows every row for that PR, OPEN ones included, because an open row carrying
    a recorded attempt is exactly where the evidence document is most worth reading: a
    fail-intent is the highest-value record here and its row never closes. An
    adversarial audit measured that filtering on status made that row unreachable from
    every reader at once, which is this reader's own defect one row-state over.

    The counterpart to the backlog. Without it the evidence column is write-only —
    MEASURED before this shipped: nothing in ``src/`` or ``scripts/`` ever SELECTed
    it, while the daily retention timer deletes closed rows at 45 days. A record
    nothing can read is not a record.

    Read-only, no worker run, no debounce — same contract and same guards as the
    backlog reader above, including the built-not-interpolated ``mode=ro`` URI.
    """
    import asyncio as _asyncio

    from genesis.db.crud import pr_verifications as verif_crud
    from genesis.env import genesis_db_path

    resolved = db_path or str(genesis_db_path())
    if not Path(resolved).exists():
        print(f"pr_verifications: no database at {resolved}")
        return
    try:
        from genesis.db.admission import database_is_fenced

        fenced = database_is_fenced(resolved)
    except Exception:
        fenced = True
    if fenced:
        print("pr_verifications: database quarantined — skipped")
        return

    async def _read() -> list[dict]:
        import aiosqlite

        uri = f"{Path(resolved).absolute().as_uri()}?mode=ro"
        async with aiosqlite.connect(uri, uri=True, timeout=10) as db:
            await db.execute("PRAGMA busy_timeout=5000")
            db.row_factory = aiosqlite.Row
            # The filter goes to SQL, never to a Python pass over a capped page:
            # paging 500 and filtering after it reported "no closed rows for PR #N"
            # about a row that WAS closed, and blamed an empty table for it.
            #
            # A SCOPED read asks "what is the state of this obligation", which includes
            # an OPEN row carrying a recorded attempt — the state where the evidence
            # document is most worth reading, since a fail-intent is the highest-value
            # record here and its row never closes. Filtering on status made that row
            # unreachable from every reader at once.
            if pr_number is not None:
                return await verif_crud.list_for_pr(db, pr_number=pr_number)
            return await verif_crud.list_closed(db, limit=_LOG_LIMIT, pr_number=None)

    rows = _asyncio.run(_read())
    if not rows:
        if pr_number is not None:
            print(
                f"pr_verifications: no rows for PR #{pr_number} — the table is empty, "
                f"pre-migration, or no obligation was ever opened for it. This read "
                f"covers OPEN rows too, so a parked attempt would have appeared here"
            )
        else:
            print(
                "pr_verifications: no closed rows — the table is empty, "
                "pre-migration, or nothing has been discharged yet"
            )
        return

    for row in rows:
        # A NULL verdict is stated, never blanked: it means the row was closed
        # before verdicts existed, or by the deterministic docs-path exemption —
        # not that a validator reached no conclusion.
        closed = row.get("status") == "closed"
        if closed:
            verdict = row.get("verdict") or "<no verdict — auto-exempt by path, or pre-verdict>"
            print(
                f"CLOSED  {row.get('repo') or '<unknown repo>'}#{row['pr_number']}  "
                f"{str(row.get('closed_at') or '')[:19]}  {verdict}"
            )
            reason = (row.get("closed_reason") or "").strip()
            if reason:
                print(f"        reason  : {reason[:160]}")
        else:
            # An OPEN row reached here only from a scoped read, and it is open BECAUSE a
            # verdict was recorded that does not discharge the obligation. Say which,
            # and how many attempts it has cost — the escalation signal.
            verdict = row.get("verdict") or "<never attempted>"
            print(
                f"OPEN    {row.get('repo') or '<unknown repo>'}#{row['pr_number']}  "
                f"{str(row.get('last_attempt_at') or row.get('merged_at') or '')[:19]}  "
                f"{verdict}"
            )
            note = (row.get("last_attempt_note") or "").strip()
            if note:
                print(f"        note    : {note[:160]}")
            if row.get("attempt_count"):
                print(f"        attempts: {row['attempt_count']}")
        evidence = row.get("evidence")
        if not evidence:
            print("        evidence: none recorded")
        elif pr_number is None:
            # An unscoped listing can carry 500 rows; printing every document would
            # bury the census this mode exists to give. The pointer is the affordance.
            print(
                f"        evidence: {len(evidence)} bytes "
                f"— read it with --verification-log --pr {row['pr_number']}"
            )
        else:
            # A SCOPED read is someone asking what was concluded, so show it. Without
            # this the column was written by the validator and read by nobody, and a
            # daily retention timer deletes closed rows at 45 days — the whole reason
            # this reader ships in the same change as the writer.
            print(f"        evidence: {len(evidence)} bytes")
            body = evidence
            if len(body) > _EVIDENCE_SHOW:
                body = (
                    body[:_EVIDENCE_SHOW]
                    + f"\n<CLIPPED at {_EVIDENCE_SHOW} of {len(evidence)} chars — "
                    f"query pr_verifications.evidence for the whole document>"
                )
            for line in body.splitlines():
                print(f"          {line}")
    # A listing whose length EQUALS its cap is a truncated read, and printing the
    # count alone lets a reader take it for a total — the same omission the backlog
    # reader states with both numbers. Say so rather than letting the two disagree
    # in silence.
    if pr_number is not None:
        # A scoped read covers OPEN rows too, so a closed-only count here told a
        # reader a parked obligation was discharged — the line directly above it said
        # OPEN. Count each status; list_for_pr is unlimited (one row per repository
        # holding that number), so there is no cap to disclose.
        n_open = sum(1 for r in rows if r.get("status") != "closed")
        print(
            f"pr_verifications: {len(rows)} row(s) for PR #{pr_number} — "
            f"{n_open} open, {len(rows) - n_open} closed ({resolved})"
        )
        return
    # A listing whose length EQUALS its cap is a truncated read, and printing the
    # count alone lets a reader take it for a total — the same omission the backlog
    # reader states with both numbers. Say so rather than letting the two disagree
    # in silence.
    if len(rows) >= _LOG_LIMIT:
        print(
            f"  <listed the {_LOG_LIMIT} most recently closed; this is a CAPPED read, "
            f"not a total — narrow with --pr, or query pr_verifications directly>"
        )
    print(f"pr_verifications: {len(rows)} closed row(s) shown ({resolved})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trigger", default="manual", choices=["session_start", "manual"])
    parser.add_argument(
        "--force",
        action="store_true",
        help="bypass the global min-interval debounce (manual/E2E runs)",
    )
    parser.add_argument(
        "--db-path",
        default=None,
        help="genesis.db path (the spawning hook passes its home-anchored "
        "resolution; default falls back to genesis.env)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="override the cursor-less enumeration window (config default: 7)",
    )
    parser.add_argument(
        "--verification-backlog",
        action="store_true",
        help="list OPEN post-merge verification obligations (never-attempted "
        "first, oldest merge first within each group) and exit — no worker run, "
        "no debounce, read-only",
    )
    parser.add_argument(
        "--verification-log",
        action="store_true",
        help="list CLOSED obligations with the verdict and evidence a validator "
        "recorded, and exit — read-only. Pair with --pr to scope to one PR",
    )
    parser.add_argument(
        "--pr",
        type=int,
        default=None,
        help="scope --verification-log to a single PR number",
    )
    args = parser.parse_args()

    # --pr scopes ONE mode. Accepted-and-ignored elsewhere, it reads as a filter that
    # was applied: a full-pulse run would report on every PR while the operator
    # believed they had narrowed it. parser.error exits 2, which is the one nonzero
    # this module's always-exit-0 contract already allows (argument parsing failing).
    if args.pr is not None and not args.verification_log:
        parser.error("--pr only scopes --verification-log; pass it alongside that flag, or drop it")

    if args.verification_backlog:
        _print_verification_backlog(args.db_path)
        return

    if args.verification_log:
        _print_verification_log(args.db_path, args.pr)
        return

    from genesis.session_awareness.repo_pulse_worker import run_pulse_worker

    outcome = asyncio.run(
        run_pulse_worker(
            trigger=args.trigger,
            force=args.force,
            db_path=args.db_path,
            lookback_days=args.lookback_days,
        )
    )
    print(f"repo_pulse_worker: {outcome}")
    if outcome.get("status") in ("failed", "timeout"):
        print(f"repo_pulse_worker: {outcome}", file=sys.stderr)


if __name__ == "__main__":
    main()
