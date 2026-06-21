"""CRUD for the capability_grants table — per-(domain, verb, risk_class) cells.

The simple-posterior competence mirror of ``crud/autonomy.py``, re-keyed to
capability cells (WS-8, PR-B).  DARK: no runtime caller writes here yet.

Deliberately does NOT touch ``autonomy_state`` — that table stays
authoritative for every existing reader (the ``autonomy_activity`` signal,
``capability_aggregator``, the direct-session circuit breaker, the earn-back
sweep) until PR-C ships the L1–L7 → cells read-out.  The asymmetric /
consequence-weighted / staleness-decay upgrades to the posterior are deferred
to PR-C/PR-D, where they are wired to real send outcomes and can be calibrated.

These functions commit their own writes — do NOT call them inside a migration
``up()`` (the runner's connection proxy forbids ``commit()``).
"""

from __future__ import annotations

import aiosqlite

from genesis.autonomy.capabilities import should_regress, transition
from genesis.autonomy.types import CellEvent, CellState


def cell_id(domain: str, verb: str, risk_class: str) -> str:
    """Deterministic cell identity — the (domain, verb, risk_class) key."""
    return f"{domain}:{verb}:{risk_class}"


def cell_posterior(successes: int, corrections: int) -> float:
    """Beta(1,1) posterior mean — a straight per-cell mirror of
    ``crud.autonomy.bayesian_posterior``.  Returns 0.5 (uninformative) with no
    evidence; a fresh cell holds at ASK regardless of this value.
    """
    total = successes + corrections
    if total == 0:
        return 0.5
    return (successes + 1) / (total + 2)


async def get_cell(
    db: aiosqlite.Connection, domain: str, verb: str, risk_class: str
) -> dict | None:
    cursor = await db.execute(
        "SELECT * FROM capability_grants WHERE id = ?",
        (cell_id(domain, verb, risk_class),),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def ensure_cell(
    db: aiosqlite.Connection,
    *,
    domain: str,
    verb: str,
    risk_class: str,
    updated_at: str,
) -> dict:
    """Insert the cell at NOT_DETERMINED if absent; return the current row."""
    cid = cell_id(domain, verb, risk_class)
    await db.execute(
        """INSERT INTO capability_grants
             (id, domain, verb, risk_class, state, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(id) DO NOTHING""",
        (cid, domain, verb, risk_class,
         CellState.NOT_DETERMINED.value, updated_at, updated_at),
    )
    await db.commit()
    row = await get_cell(db, domain, verb, risk_class)
    if row is None:  # pragma: no cover — insert-or-existing guarantees a row
        raise RuntimeError(f"capability cell {cid} missing after upsert")
    return row


async def list_all(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute(
        "SELECT * FROM capability_grants ORDER BY domain, verb, risk_class"
    )
    return [dict(r) for r in await cursor.fetchall()]


async def apply_event(
    db: aiosqlite.Connection,
    *,
    domain: str,
    verb: str,
    risk_class: str,
    event: CellEvent,
    updated_at: str,
) -> CellState:
    """Apply a state-machine event to the cell and persist the new state.

    Raises :class:`genesis.autonomy.capabilities.InvalidTransition` if the
    event is illegal from the current state.  Sets ``granted_at`` when the
    cell first reaches GRANTED.
    """
    row = await ensure_cell(
        db, domain=domain, verb=verb, risk_class=risk_class, updated_at=updated_at
    )
    new_state = transition(CellState(row["state"]), event)

    # granted_at is the decay-clock origin (PR-D): (re)stamp it on every entry
    # into GRANTED so a cell that regressed and was re-granted isn't treated as
    # older than it is.  Meaningful only while state == 'granted'.
    granted_at = row["granted_at"]
    if new_state == CellState.GRANTED:
        granted_at = updated_at

    await db.execute(
        """UPDATE capability_grants
             SET state = ?, granted_at = ?, updated_at = ?
           WHERE id = ?""",
        (new_state.value, granted_at, updated_at, row["id"]),
    )
    await db.commit()
    return new_state


async def record_success(
    db: aiosqlite.Connection,
    *,
    domain: str,
    verb: str,
    risk_class: str,
    updated_at: str,
) -> bool:
    """Increment the success counter.  Promotion stays explicit (user approve)."""
    row = await ensure_cell(
        db, domain=domain, verb=verb, risk_class=risk_class, updated_at=updated_at
    )
    cursor = await db.execute(
        """UPDATE capability_grants
             SET successes = successes + 1, last_used_at = ?, updated_at = ?
           WHERE id = ?""",
        (updated_at, updated_at, row["id"]),
    )
    await db.commit()
    return cursor.rowcount > 0


async def record_correction(
    db: aiosqlite.Connection,
    *,
    domain: str,
    verb: str,
    risk_class: str,
    updated_at: str,
) -> CellState:
    """Increment the correction counter; regress a granted cell if competence
    drops below the grant floor.  The counter increment and any regression are
    written in a SINGLE atomic UPDATE so a crash can't leave a sub-floor cell
    stuck in GRANTED.  Returns the resulting cell state.

    (Asymmetric / consequence weighting of the update is deferred to
    PR-C/PR-D — here a correction is a plain +1.)
    """
    row = await ensure_cell(
        db, domain=domain, verb=verb, risk_class=risk_class, updated_at=updated_at
    )
    new_corrections = row["corrections"] + 1

    new_state = CellState(row["state"])
    if new_state == CellState.GRANTED and should_regress(
        cell_posterior(row["successes"], new_corrections)
    ):
        new_state = transition(new_state, CellEvent.REGRESS)

    await db.execute(
        """UPDATE capability_grants
             SET corrections = ?, state = ?, updated_at = ?
           WHERE id = ?""",
        (new_corrections, new_state.value, updated_at, row["id"]),
    )
    await db.commit()
    return new_state
