"""Capability-cell state machine + competence helpers (WS-8, PR-B).

Pure, DB-free logic for the per-(domain, verb, risk_class) capability matrix
that replaces the linear L1–L7 ladder for ported channel-domains (email
first).  This module owns the *rules*; persistence lives in
``genesis.db.crud.capability_grants`` and classification in
``genesis.autonomy.classification.classify_email_action``.

DARK in PR-B: no runtime caller mutates cells yet.  Enforcement at the
``outreach_send`` chokepoint, the asymmetric / consequence-weighted competence
upgrades, and the staleness-decay *sweep* all land in PR-C/PR-D, where they are
wired to real send outcomes and can be calibrated.  ``autonomy_state`` stays
authoritative for the legacy autonomy readers until then.
"""

from __future__ import annotations

from genesis.autonomy.types import CellEvent, CellState

# Posterior below which a *granted* cell regresses back to ASK.  Mirrors the
# legacy L3 boundary (0.50 = "no longer majority-confident").  The richer
# asymmetric / consequence-weighted competence model is deferred to PR-C/PR-D.
GRANT_FLOOR = 0.50


class InvalidTransition(ValueError):
    """Raised when a (state, event) pair has no defined transition."""


# Complete transition table.  DENIED_PERMANENT is reachable ONLY by explicit
# user action (DENY_PERMANENT / REVOKE) — never by REGRESS or DECAY, so a
# competence dip or staleness can never permanently lock a cell on its own.
_TRANSITIONS: dict[tuple[CellState, CellEvent], CellState] = {
    (CellState.NOT_DETERMINED, CellEvent.CLASSIFY): CellState.ASK,
    (CellState.ASK, CellEvent.APPROVE): CellState.GRANTED,
    (CellState.ASK, CellEvent.DENY_PERMANENT): CellState.DENIED_PERMANENT,
    (CellState.GRANTED, CellEvent.REGRESS): CellState.ASK,
    (CellState.GRANTED, CellEvent.DECAY): CellState.NOT_DETERMINED,
    (CellState.GRANTED, CellEvent.REVOKE): CellState.DENIED_PERMANENT,
}


def transition(state: CellState, event: CellEvent) -> CellState:
    """Return the next cell state for ``event`` applied to ``state``.

    Raises :class:`InvalidTransition` if the event is illegal from the
    current state (e.g. approving a cell that was never asked).
    """
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransition(
            f"no capability-cell transition: {state} --{event}--> ?"
        ) from None


def can_transition(state: CellState, event: CellEvent) -> bool:
    """True iff ``(state, event)`` has a defined transition."""
    return (state, event) in _TRANSITIONS


def should_regress(posterior: float, *, floor: float = GRANT_FLOOR) -> bool:
    """True iff a granted cell's competence has fallen below the grant floor."""
    return posterior < floor
