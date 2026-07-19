"""CRUD for ``calibration_cells`` + ``calibration_cell_history`` (WS-2 P3).

Storage layer only — the aggregation math lives in ``genesis.ledger.cells``.

Convention notes: functions commit their own writes (never call these inside
a migration ``up()``); ALL validation happens before the first execute so a
raise can never strand an uncommitted row on the shared connection; timestamps
go through ``canonical_iso``.

``replace_cells`` deliberately uses **upsert-then-prune**, not
DELETE-then-INSERT: ``SerializedConnection`` locks per *method*, so a foreign
coroutine's ``commit()`` can interleave between two statements and durably
commit the intermediate state. With upsert-then-prune the intermediate states
are "mixed old/new cells" — never an empty table — so cross-process readers
(the ``calibration_status`` MCP) and same-connection readers (perception,
dashboard) always see a complete surface, and a crash mid-rebuild loses
nothing but staleness.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import aiosqlite

from genesis.db.timeutil import canonical_iso

_PROVENANCES = frozenset({"stated", "policy_prior", "all"})
_STATUSES = frozenset({"ok", "thin", "unknown"})

# Column order used by both the upsert and the history snapshot.
_CELL_COLUMNS = (
    "domain, action_class, metric, provenance, window_days, n, n_mechanical, "
    "base_rate, mean_confidence, brier, reliability, resolution, uncertainty, "
    "ece, shrunk_estimate, status, computed_at"
)
_STAT_KEYS = (
    "base_rate",
    "mean_confidence",
    "brier",
    "reliability",
    "resolution",
    "uncertainty",
    "ece",
    "shrunk_estimate",
)


def _validate(cells: list[dict]) -> None:
    for cell in cells:
        if cell["provenance"] not in _PROVENANCES:
            raise ValueError(
                f"provenance must be one of {sorted(_PROVENANCES)}, got {cell['provenance']!r}"
            )
        if cell["status"] not in _STATUSES:
            raise ValueError(f"status must be one of {sorted(_STATUSES)}, got {cell['status']!r}")
        if not isinstance(cell["n"], int) or cell["n"] < 0:
            raise ValueError(f"n must be a non-negative int, got {cell['n']!r}")


async def replace_cells(db: aiosqlite.Connection, cells: list[dict], *, now: datetime) -> int:
    """Upsert this pass's cells, then prune cells the pass did not produce.

    Returns the number of cells written. Empty ``cells`` on an empty source is
    the fresh-install no-op: nothing upserted, and the prune deletes nothing
    because the table is already empty. (If the source genuinely shrank to
    zero, pruning everything is CORRECT — stale cells lying about vanished
    data would be worse.)
    """
    _validate(cells)
    computed_at = canonical_iso(now.isoformat())
    if cells:
        await db.executemany(
            f"INSERT OR REPLACE INTO calibration_cells ({_CELL_COLUMNS}) "  # noqa: S608 — column list is a module constant
            f"VALUES ({', '.join('?' for _ in range(17))})",
            [
                (
                    c["domain"],
                    c["action_class"],
                    c["metric"],
                    c["provenance"],
                    c["window_days"],
                    c["n"],
                    c["n_mechanical"],
                    *(c.get(k) for k in _STAT_KEYS),
                    c["status"],
                    computed_at,
                )
                for c in cells
            ],
        )
        await db.execute("DELETE FROM calibration_cells WHERE computed_at != ?", (computed_at,))
    else:
        await db.execute("DELETE FROM calibration_cells")
    await db.commit()
    return len(cells)


async def append_history(db: aiosqlite.Connection, cells: list[dict], *, now: datetime) -> int:
    """One trend snapshot per cell. Returns the number of rows appended."""
    _validate(cells)
    snapshot_at = canonical_iso(now.isoformat())
    if not cells:
        return 0
    await db.executemany(
        "INSERT INTO calibration_cell_history "
        "(id, domain, action_class, metric, provenance, window_days, "
        " n, brier, reliability, resolution, ece, status, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                uuid.uuid4().hex[:16],
                c["domain"],
                c["action_class"],
                c["metric"],
                c["provenance"],
                c["window_days"],
                c["n"],
                c.get("brier"),
                c.get("reliability"),
                c.get("resolution"),
                c.get("ece"),
                c["status"],
                snapshot_at,
            )
            for c in cells
        ],
    )
    await db.commit()
    return len(cells)


async def prune_history(db: aiosqlite.Connection, *, before: str) -> int:
    """Delete snapshots older than ``before`` (ISO). Returns rows deleted."""
    cursor = await db.execute(
        "DELETE FROM calibration_cell_history WHERE snapshot_at < ?", (before,)
    )
    await db.commit()
    return cursor.rowcount


async def list_cells(
    db: aiosqlite.Connection,
    *,
    domain: str | None = None,
    provenance: str | None = None,
    window_days: int | None = None,
    exclude_ego: bool = False,
) -> list[dict]:
    """Read cells with optional filters.

    ``domain`` matches exactly OR as a dotted prefix (``outreach`` matches
    ``outreach.general``). ``exclude_ego`` drops ``ego`` and ``ego.*`` domains
    (design §4.2 — ego calibration is scoped to the flag-guarded ego-context
    section, never the general perception surface).
    """
    clauses: list[str] = []
    params: list[object] = []
    if domain:
        clauses.append("(domain = ? OR domain LIKE ?)")
        params.extend([domain, f"{domain}.%"])
    if provenance:
        clauses.append("provenance = ?")
        params.append(provenance)
    if window_days is not None:
        clauses.append("window_days = ?")
        params.append(window_days)
    if exclude_ego:
        clauses.append("NOT (domain = 'ego' OR domain LIKE 'ego.%')")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    cursor = await db.execute(
        f"SELECT {_CELL_COLUMNS} FROM calibration_cells {where} "  # noqa: S608 — column list + assembled WHERE are module-local
        "ORDER BY domain, action_class, metric, provenance, window_days",
        params,
    )
    return [dict(r) for r in await cursor.fetchall()]


async def list_history(
    db: aiosqlite.Connection,
    *,
    domain: str,
    metric: str | None = None,
    limit: int = 90,
) -> list[dict]:
    """Trend snapshots for a domain, newest first. Rides ``idx_cch_cell_time``."""
    clauses = ["domain = ?"]
    params: list[object] = [domain]
    if metric:
        clauses.append("metric = ?")
        params.append(metric)
    params.append(limit)
    cursor = await db.execute(
        "SELECT id, domain, action_class, metric, provenance, window_days, "
        "n, brier, reliability, resolution, ece, status, snapshot_at "
        f"FROM calibration_cell_history WHERE {' AND '.join(clauses)} "  # noqa: S608 — assembled WHERE is module-local
        "ORDER BY snapshot_at DESC LIMIT ?",
        params,
    )
    return [dict(r) for r in await cursor.fetchall()]
