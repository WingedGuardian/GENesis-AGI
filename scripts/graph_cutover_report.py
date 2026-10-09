#!/usr/bin/env python3
"""Is the FalkorDB graph engine ready to go default-on?

Prints the cutover verdict computed by ``genesis.memory.graph_cutover`` from the
durable rows: the traversal telemetry (``graph_traverse``), the hourly
memory-server census (``graph_traverse_census``) and the local lost-writes file.

    PASS          14 days on the clock with no fallback, enough falkordb traffic
    NOT_YET       nothing wrong, not enough clock or traffic yet; says what is short
    INCONCLUSIVE  some of the evidence may be missing; says what, with its time

Exit status: 0 PASS, 1 NOT_YET, 2 INCONCLUSIVE, 3 the report itself failed
(never 1, so a failure can never read as NOT_YET).

Read-only: the database is opened with ``mode=ro`` and every row of both event
types is read (no row limit: a capped read could hide the one fallback that
matters). ``--now`` evaluates as of another time, for tests and replays.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

_EXIT = {"PASS": 0, "NOT_YET": 1, "INCONCLUSIVE": 2}
_FAILED = 3


class _Parser(argparse.ArgumentParser):
    """argparse exits 2 on a usage error, which here would read as INCONCLUSIVE."""

    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(_FAILED, f"{self.prog}: error: {message}\n")


def read_rows(db_path: Path, event_type: str) -> list[tuple[str, str | None]]:
    with contextlib.closing(
        sqlite3.connect(f"{Path(db_path).resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    ) as conn:
        return list(
            conn.execute(
                "SELECT timestamp, metrics_json FROM eval_events WHERE event_type = ?"
                " ORDER BY timestamp",
                (event_type,),
            )
        )


def read_lost_writes(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return []


def render(v) -> str:
    out = [f"FalkorDB default-on cutover: {v.status}"]
    if v.clock_start:
        out.append(f"  clock started {v.clock_start.isoformat()} ({v.days_on_clock} days)")
    else:
        out.append("  clock not started")
    share = f"{v.cancelled / v.traversals:.1%}" if v.traversals else "n/a"
    out.append(
        f"  last 14 days: {v.falkordb_traversals} served by falkordb, traffic on "
        f"{v.days_with_traffic} days, {v.cancelled} of {v.traversals} cancelled ({share})"
    )
    last = v.last_census.isoformat() if v.last_census else "never"
    out.append(f"  census: {v.census_rows} rows, last {last}")
    for reason in v.reasons:
        out.append(f"  - {reason}")
    if v.resets:
        out.append("  clock resets (newest last):")
        for r in v.resets[-10:]:
            who = " ".join(str(r[k]) for k in ("proc", "caller") if r.get(k))
            old = "" if r.get("in_window", True) else "  (before the window)"
            out.append(f"    {r['at']}  {r['why']}{'  ' + who if who else ''}{old}")
        if len(v.resets) > 10:
            out.append(f"    ({len(v.resets) - 10} earlier, see --json)")
    out.append(
        "  residual: a database AND lost-writes-file failure at the same moment, or a"
        " process killed in the instant a break is being written, can go uncounted."
    )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    from genesis.env import genesis_db_path
    from genesis.memory import graph_census, graph_cutover, graph_telemetry

    ap = _Parser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", type=Path, default=None, help="database (default: the live one)")
    ap.add_argument("--lost-writes", type=Path, default=None, help="lost-writes file")
    ap.add_argument("--now", default=None, help="evaluate as of this ISO-8601 time")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)
    if args.db is not None and args.lost_writes is None:
        # The default lost-writes file belongs to THIS install; pairing it with
        # another install's database would mix two installs' evidence.
        ap.error("--db needs --lost-writes (the lost-writes file of the same install)")

    now = graph_cutover.parse_ts(args.now) if args.now else datetime.now(UTC)
    if now is None:
        ap.error(f"--now is not an ISO-8601 time: {args.now!r}")
    db = args.db or genesis_db_path()
    verdict = graph_cutover.evaluate(
        read_rows(db, graph_telemetry.TELEMETRY_EVENT_TYPE),
        read_rows(db, graph_census.CENSUS_EVENT_TYPE),
        read_lost_writes(args.lost_writes or graph_telemetry.lost_writes_path()),
        now,
    )
    print(json.dumps(verdict.to_json(), indent=2) if args.json else render(verdict))
    return _EXIT[verdict.status]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - any failure must not exit 1 (NOT_YET)
        print(f"graph_cutover_report failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(_FAILED)
