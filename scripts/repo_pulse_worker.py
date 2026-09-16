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
        help="list OPEN post-merge verification obligations (oldest merge "
        "first) and exit — no worker run, no debounce, read-only",
    )
    args = parser.parse_args()

    if args.verification_backlog:
        _print_verification_backlog(args.db_path)
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
