"""CRUD for the capability_grants table — per-(domain, verb, risk_class) cells.

The per-cell competence model, re-keyed from ``crud/autonomy.py`` (WS-8).  Live
since PR-C (the email gate's resolution watcher records success/correction on
cells); PR-D adds the consequence-weighted RE-earn posterior, the deterministic
GRANTED→ASK demotion ("trust is easy to lose"), and promotion-candidate
detection.

Deliberately does NOT touch ``autonomy_state`` — that table stays authoritative
for every existing (non-email) reader (the ``autonomy_activity`` signal,
``capability_aggregator``, the direct-session circuit breaker, the legacy
earn-back sweep).  Email autonomy is cell-only.

These functions commit their own writes — do NOT call them inside a migration
``up()`` (the runner's connection proxy forbids ``commit()``).
"""

from __future__ import annotations

import aiosqlite

from genesis.autonomy.capabilities import transition
from genesis.autonomy.types import CellEvent, CellState, RiskClass

#: Owner-approved-promotion bar (mirrors the legacy L-threshold): a cell is
#: PROPOSABLE for promotion only at/above this re-earn posterior AND with real
#: evidence behind it.
PROMOTE_THRESHOLD = 0.70
#: Minimum approved successes before a cell may be PROPOSED for promotion —
#: closes the low-N trap (one lucky success is not earned trust).
MIN_PROMOTE_N = 5

#: Consequence weights by risk_class (WS-8 PR-D).  A correction's damage to a
#: cell's RE-earn posterior is severity-proportional.  GATE-DERIVED from the
#: classified risk_class — NEVER supplied by the LLM.  (financial never grants,
#: but is defined for completeness.)
_CORRECTION_WEIGHTS: dict[RiskClass, float] = {
    RiskClass.STANDARD: 1.0,
    RiskClass.IDENTITY: 1.5,
    RiskClass.BULK: 2.0,
    RiskClass.FINANCIAL: 3.0,
}


def correction_weight(risk_class: str) -> float:
    """Severity weight for a correction on a cell of this risk_class (≥ 1.0)."""
    try:
        return _CORRECTION_WEIGHTS[RiskClass(risk_class)]
    except (ValueError, KeyError):
        return 1.0


def cell_id(domain: str, verb: str, risk_class: str) -> str:
    """Deterministic cell identity — the (domain, verb, risk_class) key."""
    return f"{domain}:{verb}:{risk_class}"


def cell_posterior(
    successes: int, corrections: int, weighted_corrections: float = 0.0
) -> float:
    """Beta(1,1) posterior mean, re-earn flavour (WS-8 PR-D).

    ``weighted_corrections`` is the Σ of consequence-weighted corrections; when
    present it replaces the raw correction count in the denominator so a heavier
    past harm leaves a deeper crater (more clean successes needed to re-promote).
    With ``weighted_corrections == 0.0`` this is the plain mirror of
    ``crud.autonomy.bayesian_posterior``.  Returns 0.5 (uninformative) with no
    evidence; a fresh cell holds at ASK regardless of this value.
    """
    eff_corrections = max(float(corrections), weighted_corrections)
    total = successes + eff_corrections
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
    consequence_weight: float | None = None,
) -> CellState:
    """Record a correction on a cell and, if it was GRANTED, DEMOTE it to ASK.

    WS-8 PR-D — "trust is hard to earn, easy to lose": a GRANTED cell regresses
    to ASK on ANY confirmed correction (deterministic, NOT posterior-gated — one
    bad autonomous send means autonomy stops NOW, not "if the running average
    dips").  The severity-weighted ``weighted_corrections`` accumulator governs
    how hard the cell is to RE-earn, not whether it demotes.

    ``consequence_weight`` defaults to the risk-class severity (gate-derived;
    NEVER LLM-supplied).  The counter bump and any regression land in ONE atomic
    UPDATE so a crash can't leave a corrected cell stuck GRANTED.  Returns the
    resulting cell state.
    """
    row = await ensure_cell(
        db, domain=domain, verb=verb, risk_class=risk_class, updated_at=updated_at
    )
    weight = (
        consequence_weight
        if consequence_weight is not None
        else correction_weight(risk_class)
    )
    new_corrections = row["corrections"] + 1
    new_weighted = (row["weighted_corrections"] or 0.0) + weight

    new_state = CellState(row["state"])
    granted_at = row["granted_at"]
    if new_state == CellState.GRANTED:
        new_state = transition(new_state, CellEvent.REGRESS)  # any correction → ASK
        granted_at = None  # clear the decay-clock origin on demotion

    await db.execute(
        """UPDATE capability_grants
             SET corrections = ?, weighted_corrections = ?, state = ?,
                 granted_at = ?, updated_at = ?
           WHERE id = ?""",
        (new_corrections, new_weighted, new_state.value, granted_at,
         updated_at, row["id"]),
    )
    await db.commit()
    return new_state


async def touch_used(
    db: aiosqlite.Connection,
    *,
    domain: str,
    verb: str,
    risk_class: str,
    used_at: str,
) -> bool:
    """Mark a GRANTED cell as just-used WITHOUT recording a success.

    An autonomous send under a GRANTED cell is not a competence signal (no human
    confirmed it was good) — competence-in-GRANTED is measured by the ABSENCE of
    corrections.  This only bumps ``last_used_at`` so the staleness-decay sweep
    can tell an active grant from an idle one.
    """
    cursor = await db.execute(
        "UPDATE capability_grants SET last_used_at = ?, updated_at = ? WHERE id = ?",
        (used_at, used_at, cell_id(domain, verb, risk_class)),
    )
    await db.commit()
    return cursor.rowcount > 0


async def detect_promotable_cells(
    db: aiosqlite.Connection,
    *,
    min_successes: int = MIN_PROMOTE_N,
    threshold: float = PROMOTE_THRESHOLD,
) -> list[dict]:
    """ASK cells with enough evidence to PROPOSE for owner-approved promotion.

    A cell qualifies when it has ≥ ``min_successes`` approved successes AND its
    severity-weighted re-earn posterior is ≥ ``threshold``.  Recommend-only:
    this never promotes — it surfaces candidates for the cadence to propose and
    the owner to approve.  Each returned row carries a computed ``posterior``.
    """
    cursor = await db.execute(
        "SELECT * FROM capability_grants WHERE state = ?", (CellState.ASK.value,)
    )
    out: list[dict] = []
    for r in await cursor.fetchall():
        row = dict(r)
        if row["successes"] < min_successes:
            continue
        posterior = cell_posterior(
            row["successes"], row["corrections"], row.get("weighted_corrections") or 0.0
        )
        if posterior >= threshold:
            row["posterior"] = posterior
            out.append(row)
    return out
