#!/usr/bin/env python3
"""Label-aware GC for attention snapshots (home ``ambient_*.db`` + OMI ``omi_*.db``).

Deletes a snapshot db ONLY when it is older than its retention window AND no LABELED
attention_event references it (``acceptance_signal IS NOT NULL``). A referenced-and-
labeled snapshot is kept FOREVER: purging it makes its labeled events review-read-only
permanently — ``genesis.attention.sources.resolve_window_text`` returns None → the route
410s, and that labeled event can never be revealed again. See sources.py:98.

Fail-safe by construction: if the DB is missing / unreadable, or the label check errors,
the snapshot is KEPT. Only an explicit "old AND provably unreferenced-or-unlabeled" verdict
deletes. Age comes from the deterministic filename stamp (falling back to file mtime).

Windows (defaults): home snapshots > 60d, OMI snapshots > 14d (off-prem text, shorter).

Run daily via scripts/disk_hygiene.sh (also runnable by hand). Stdlib-only — NO genesis
package imports (mirrors worktree_lifecycle.py / disk_reclaim.py).

Usage:
    attention_snapshot_gc.py                 # prune per the default windows
    attention_snapshot_gc.py --dry-run       # show WOULD-DELETE, change nothing
    attention_snapshot_gc.py --home-days 90 --omi-days 21
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

_DEFAULT_SNAPS = Path.home() / ".genesis" / "attention" / "snapshots"
_DEFAULT_DB = Path.home() / "genesis" / "data" / "genesis.db"

# file ambient_<ID>.db  ->  attention_events.snapshot_id == bare <ID> (verified live)
_HOME_RE = re.compile(r"^ambient_(?P<id>\d{8}T\d{6}Z)\.db$")
_OMI_RE = re.compile(r"^omi_(?P<id>\d{8})\.db$")


def _log(msg: str) -> None:
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")
    print(f"{ts} snapshot-gc {msg}", flush=True)


def _bare_id(path: Path) -> str:
    """Snapshot id as stored in ``attention_events.snapshot_id``.

    MUST mirror ``genesis.attention.runner._snapshot_id_from_path`` exactly — it does the
    same ``removeprefix("ambient_")``, so the id we look up equals the id the runner
    persisted. The ``omi_`` prefix is deliberately NOT stripped on either side: an
    ``omi_YYYYMMDD.db`` snapshot is stored AND looked up as ``omi_YYYYMMDD``. Stripping it
    here would look up a bare date that the runner never wrote → a labeled OMI snapshot
    could be deleted. test_bare_id_matches_runner_snapshot_id_derivation locks this contract.
    """
    return path.stem.removeprefix("ambient_")


def _age_days(path: Path, now: float) -> float:
    """Age from the filename stamp; fall back to file mtime if unparseable."""
    m = _HOME_RE.match(path.name)
    if m:
        try:
            dt = datetime.strptime(m["id"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            return (now - dt.timestamp()) / 86400
        except ValueError:
            pass
    m = _OMI_RE.match(path.name)
    if m:
        try:
            dt = datetime.strptime(m["id"], "%Y%m%d").replace(tzinfo=UTC)
            return (now - dt.timestamp()) / 86400
        except ValueError:
            pass
    return (now - path.stat().st_mtime) / 86400


def _is_labeled_referenced(conn: sqlite3.Connection, sid: str, raw_stem: str) -> bool:
    """True if a LABELED event references this snapshot. Errors -> True (fail-safe keep).

    Queries both the bare id and the raw filename stem so a snapshot_id-format surprise
    can never make a labeled snapshot look unreferenced.
    """
    try:
        cur = conn.execute(
            "SELECT 1 FROM attention_events "
            "WHERE snapshot_id IN (?, ?) AND acceptance_signal IS NOT NULL LIMIT 1",
            (sid, raw_stem),
        )
        return cur.fetchone() is not None
    except sqlite3.Error as e:
        _log(f"WARN label check failed for {sid!r} ({e}) — keeping (fail-safe)")
        return True


def run_gc(
    *,
    snapshots_dir: Path,
    db_path: Path,
    home_days: int = 60,
    omi_days: int = 14,
    dry_run: bool = False,
    now: float | None = None,
) -> int:
    """Delete old, unlabeled-or-unreferenced snapshot dbs. Returns the delete count."""
    now = now if now is not None else datetime.now(UTC).timestamp()
    snapshots_dir = Path(snapshots_dir)
    db_path = Path(db_path)
    if not snapshots_dir.is_dir():
        return 0

    # No DB / unreadable DB -> cannot verify labels -> keep EVERYTHING (fail-safe).
    if not db_path.exists():
        _log(f"WARN db not found at {db_path} — keeping all snapshots (cannot verify labels)")
        return 0
    # Read-only, direct SQL by design: this is a stdlib-only maintenance script (like
    # worktree_lifecycle.py / backup.sh) and cannot import the async genesis.db.crud layer.
    # The "crud-layer only" convention guards WRITES to genesis.db; this only ever reads
    # (mode=ro), so the direct query is a deliberate, blessed exception.
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as e:
        _log(f"WARN cannot open db {db_path} ({e}) — keeping all snapshots")
        return 0

    deleted = 0
    try:
        for p in sorted(snapshots_dir.glob("*.db")):
            is_home = bool(_HOME_RE.match(p.name))
            is_omi = bool(_OMI_RE.match(p.name))
            if not (is_home or is_omi):
                continue  # unknown file — never touch
            window = home_days if is_home else omi_days
            if _age_days(p, now) <= window:
                continue  # within retention window — keep
            sid, raw_stem = _bare_id(p), p.stem
            if _is_labeled_referenced(conn, sid, raw_stem):
                _log(f"KEEP {p.name}: labeled-referenced (review-read-only)")
                continue
            if dry_run:
                _log(f"WOULD DELETE {p.name}: >{window}d, no labeled refs")
                continue
            try:
                p.unlink()
                deleted += 1
                _log(f"DELETE {p.name}: >{window}d, no labeled refs")
            except OSError as e:
                _log(f"ERROR deleting {p.name}: {e}")
    finally:
        conn.close()
    return deleted


def main() -> int:
    ap = argparse.ArgumentParser(description="Label-aware attention snapshot GC")
    ap.add_argument("--snapshots-dir", default=str(_DEFAULT_SNAPS))
    ap.add_argument("--db", default=str(_DEFAULT_DB), help="genesis.db path")
    ap.add_argument("--home-days", type=int, default=60)
    ap.add_argument("--omi-days", type=int, default=14)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    n = run_gc(
        snapshots_dir=Path(a.snapshots_dir).expanduser(),
        db_path=Path(a.db).expanduser(),
        home_days=a.home_days,
        omi_days=a.omi_days,
        dry_run=a.dry_run,
    )
    _log(f"done: {'would delete' if a.dry_run else 'deleted'} {n} snapshot(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
