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

import logging
from datetime import datetime, timedelta

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


def cell_posterior(successes: int, corrections: int, weighted_corrections: float = 0.0) -> float:
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
        (cid, domain, verb, risk_class, CellState.NOT_DETERMINED.value, updated_at, updated_at),
    )
    await db.commit()
    row = await get_cell(db, domain, verb, risk_class)
    if row is None:  # pragma: no cover — insert-or-existing guarantees a row
        raise RuntimeError(f"capability cell {cid} missing after upsert")
    return row


async def list_all(db: aiosqlite.Connection) -> list[dict]:
    cursor = await db.execute("SELECT * FROM capability_grants ORDER BY domain, verb, risk_class")
    return [dict(r) for r in await cursor.fetchall()]


async def list_granted(db: aiosqlite.Connection) -> list[dict]:
    """All GRANTED cells — what Genesis is authorized to do autonomously (the
    'standing autonomy' pane of the owner-visibility Activity tab)."""
    cursor = await db.execute(
        "SELECT * FROM capability_grants WHERE state = ? ORDER BY domain, verb, risk_class",
        (CellState.GRANTED.value,),
    )
    return [dict(r) for r in await cursor.fetchall()]


def _autonomy_enforce_refuses(origin_class: str) -> bool:
    """WS-3 B4 gate-3 ENFORCE: refuse grant-evidence/state writes whose
    provenance is blockable. All current callers are owner-gated
    (owner/first_party), so under enforce this is pure armor — zero behavior
    change today; it exists so a FUTURE caller that threads external
    provenance cannot mutate autonomy state. Fail-OPEN on config errors (the
    write proceeds and the shadow emit still records it)."""
    from genesis.security import immunity

    try:
        return immunity.gate_mode("autonomy") == "enforce" and immunity.is_blockable(
            origin_class
        )
    except Exception:
        logging.getLogger(__name__).debug(
            "autonomy enforce check failed open", exc_info=True
        )
        return False


async def _emit_autonomy_gate(
    db: aiosqlite.Connection,
    *,
    fn: str,
    origin_class: str,
    domain: str,
    verb: str,
    risk_class: str,
    extra: dict | None = None,
) -> None:
    """WS-3 B1 gate-3 (autonomy) shadow emit — the SINGLE choke for grant
    evidence/state mutations. Self-guarding + fail-open (record_would_block
    swallows everything); owner/first_party origins and the kill switch produce
    NO row. Detail carries the cell key only — never content."""
    from genesis.security import immunity_shadow

    detail: dict = {"cell": f"{domain}:{verb}:{risk_class}"}
    if extra:
        detail.update(extra)
    await immunity_shadow.record_would_block(
        gate="autonomy",
        source_kind="grant_evidence",
        source_ref=f"db/crud/capability_grants.py::{fn}",
        process="server",
        blockable_count=1,
        origin_class=origin_class,
        db=db,
        detail=detail,
    )


async def apply_event(
    db: aiosqlite.Connection,
    *,
    domain: str,
    verb: str,
    risk_class: str,
    event: CellEvent,
    updated_at: str,
    origin_class: str,
) -> CellState:
    """Apply a state-machine event to the cell and persist the new state.

    Raises :class:`genesis.autonomy.capabilities.InvalidTransition` if the
    event is illegal from the current state.  Sets ``granted_at`` when the
    cell first reaches GRANTED.

    ``origin_class`` (REQUIRED — WS-3 gate-3) is the provenance of whatever
    prompted this state change: ``owner`` for owner decisions
    (approve/reject), ``first_party`` for Genesis's own deterministic
    guards/classifiers. Required, not defaulted, so every future caller must
    STATE provenance — a silent first_party default would be a permanently
    inert gate. Under gate-3 ENFORCE a blockable origin is REFUSED (no
    transition; returns the CURRENT state) — the emit records the attempt.
    """
    if _autonomy_enforce_refuses(origin_class):
        await _emit_autonomy_gate(
            db, fn="apply_event", origin_class=origin_class,
            domain=domain, verb=verb, risk_class=risk_class,
            extra={"event": event.value, "refused": True},
        )
        # Refusal must not mutate autonomy state — not even by creating the
        # cell (a pre-refusal ensure_cell would let external provenance seed
        # NOT_DETERMINED rows). Read-only: report the existing state, or the
        # state machine's default when no cell exists.
        row = await get_cell(db, domain, verb, risk_class)
        return CellState(row["state"]) if row else CellState.NOT_DETERMINED
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
    await _emit_autonomy_gate(
        db,
        fn="apply_event",
        origin_class=origin_class,
        domain=domain,
        verb=verb,
        risk_class=risk_class,
        extra={"event": event.value},
    )
    return new_state


async def record_success(
    db: aiosqlite.Connection,
    *,
    domain: str,
    verb: str,
    risk_class: str,
    updated_at: str,
    origin_class: str,
) -> bool:
    """Increment the success counter.  Promotion stays explicit (user approve).

    ``origin_class`` (REQUIRED — WS-3 gate-3): provenance of the success
    evidence. See :func:`apply_event`. Under gate-3 ENFORCE a blockable
    origin is REFUSED (returns False, no counter bump) — the emit still
    records the attempt (the enforce-mode row is the block ledger).
    """
    if _autonomy_enforce_refuses(origin_class):
        await _emit_autonomy_gate(
            db, fn="record_success", origin_class=origin_class,
            domain=domain, verb=verb, risk_class=risk_class,
            extra={"refused": True},
        )
        return False
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
    await _emit_autonomy_gate(
        db,
        fn="record_success",
        origin_class=origin_class,
        domain=domain,
        verb=verb,
        risk_class=risk_class,
    )
    return cursor.rowcount > 0


async def record_correction(
    db: aiosqlite.Connection,
    *,
    domain: str,
    verb: str,
    risk_class: str,
    updated_at: str,
    origin_class: str,
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
    resulting cell state. Under gate-3 ENFORCE a blockable origin is REFUSED
    (no mutation; returns the CURRENT state) — the emit records the attempt.
    """
    if _autonomy_enforce_refuses(origin_class):
        await _emit_autonomy_gate(
            db, fn="record_correction", origin_class=origin_class,
            domain=domain, verb=verb, risk_class=risk_class,
            extra={"refused": True},
        )
        # No mutation on refusal — see apply_event: read-only state report,
        # never a cell insert.
        row = await get_cell(db, domain, verb, risk_class)
        return CellState(row["state"]) if row else CellState.NOT_DETERMINED
    row = await ensure_cell(
        db, domain=domain, verb=verb, risk_class=risk_class, updated_at=updated_at
    )
    weight = consequence_weight if consequence_weight is not None else correction_weight(risk_class)
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
        (new_corrections, new_weighted, new_state.value, granted_at, updated_at, row["id"]),
    )
    await db.commit()
    await _emit_autonomy_gate(
        db,
        fn="record_correction",
        origin_class=origin_class,
        domain=domain,
        verb=verb,
        risk_class=risk_class,
    )
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


async def decay_stale_cells(
    db: aiosqlite.Connection,
    *,
    now: str,
    half_life_days: int = 90,
) -> list[str]:
    """Decay GRANTED cells idle longer than the half-life back to NOT_DETERMINED
    (the ``DECAY`` transition, applied in bulk).

    Staleness is measured from the most recent activity (``last_used_at``, else
    the grant time).  A decayed cell holds again on its next send (CLASSIFY→ASK),
    so a long-unused standing autonomy lapses rather than entrenching.  Atomic
    ``UPDATE…RETURNING``.  Returns the decayed cell ids.
    """
    cutoff = (datetime.fromisoformat(now) - timedelta(days=half_life_days)).isoformat()
    cursor = await db.execute(
        """UPDATE capability_grants
             SET state = ?, granted_at = NULL, last_decayed_at = ?, updated_at = ?
           WHERE state = ?
             AND granted_at IS NOT NULL
             AND COALESCE(last_used_at, granted_at) < ?
           RETURNING id""",
        (CellState.NOT_DETERMINED.value, now, now, CellState.GRANTED.value, cutoff),
    )
    rows = await cursor.fetchall()
    await db.commit()
    return [r[0] for r in rows]
