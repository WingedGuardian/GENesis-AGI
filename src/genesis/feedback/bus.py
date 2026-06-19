"""feedback/bus.py — the Outcome Bus write path + signal taxonomy.

``record_outcome`` is the single, fire-and-forget entry point for recording what
happened after Genesis acted. It applies the signal taxonomy (controlled
``signal_type`` vocabulary + tier policy) and NEVER raises — instrumentation must
never break a production path (modeled on ``eval/j9_hooks.py``).

Signal-quality hierarchy (the whole point of the bus):
- **Tier 1 — ground truth.** Post-execution outcome: did the dispatched work
  actually succeed / pass deliverable verification. Outranks user approval.
- **Tier 2 — informative.** A rationale-bearing signal (a rejection WITH a
  reason, an outreach reply, a triage correction). Discounted, never absolute.
- **Tier 3 — coverage only.** Bare status / lifecycle / weak implicit signals
  (a timeout is not disapproval; a dispatch is not an outcome). ~zero quality
  weight; recorded so the loop closes, kept out of any quality denominator.

Anti-timidity: nothing here teaches the ego to propose *less*. Tiering measures
signal quality, not desirability of proposing.
"""

from __future__ import annotations

import logging

import aiosqlite

from genesis.db.crud import outcome_events as _crud

logger = logging.getLogger(__name__)


class SignalType:
    """Controlled ``signal_type`` vocabulary.

    Distinct values per event class guarantee that two different signals on the
    SAME ref (e.g. a user_decision and a later execution_outcome on one proposal)
    never collide on the unique key ``(source, ref_type, ref_id, signal_type)`` —
    so the bus can never silently drop the higher-tier ground-truth signal.
    """

    # Tier 1 — post-execution ground truth
    EXECUTION_OUTCOME = "execution_outcome"
    VERIFICATION_FAILED = "verification_failed"
    # Tier 2 — informative (rationale-bearing)
    USER_DECISION = "user_decision"          # tier 2 WITH a rationale, else tier 3
    OUTREACH_REPLY = "outreach_reply"
    TRIAGE_CORRECTION = "triage_correction"
    # Tier 3 — coverage / lifecycle / weak implicit
    DISPATCH = "dispatch"
    LIFECYCLE_TABLED = "lifecycle_tabled"
    LIFECYCLE_WITHDRAWN = "lifecycle_withdrawn"
    OUTREACH_IMPLICIT = "outreach_implicit"
    MEMORY_INFLUENCE = "memory_influence"


# Default tier per signal_type. USER_DECISION is special-cased: a bare decision
# is coverage-only (T3), but a decision carrying a rationale is informative (T2).
_DEFAULT_TIER: dict[str, int] = {
    SignalType.EXECUTION_OUTCOME: 1,
    SignalType.VERIFICATION_FAILED: 1,
    SignalType.USER_DECISION: 3,
    SignalType.OUTREACH_REPLY: 2,
    SignalType.TRIAGE_CORRECTION: 2,
    SignalType.DISPATCH: 3,
    SignalType.LIFECYCLE_TABLED: 3,
    SignalType.LIFECYCLE_WITHDRAWN: 3,
    SignalType.OUTREACH_IMPLICIT: 3,
    SignalType.MEMORY_INFLUENCE: 3,
}

KNOWN_SIGNAL_TYPES = frozenset(_DEFAULT_TIER)


def default_tier(signal_type: str, *, has_rationale: bool = False) -> int:
    """Resolve the signal tier from the taxonomy.

    A ``user_decision`` is upgraded from coverage (T3) to informative (T2) only
    when it carries a rationale — the user's *why* is the best signal we get from
    a decision, and even then it is discounted, never treated as ground truth.
    """
    if signal_type == SignalType.USER_DECISION:
        return 2 if has_rationale else 3
    return _DEFAULT_TIER.get(signal_type, 3)


async def record_outcome(
    db: aiosqlite.Connection,
    *,
    source: str,
    ref_type: str,
    ref_id: str,
    signal_type: str,
    signal_tier: int | None = None,
    domain: str | None = None,
    signal_class: str = "implicit",
    polarity: str | None = None,
    value: float | None = None,
    stated_confidence: float | None = None,
    prediction_error: float | None = None,
    reason: str | None = None,
    reason_text: str | None = None,
    metadata: dict | None = None,
    harvested_from: str | None = None,
    occurred_at: str | None = None,
) -> str | None:
    """Record one outcome event — fire-and-forget, never raises.

    The single write path for the bus (live emit hooks AND the harvester). If
    ``signal_tier`` is not given it is derived from the taxonomy; for a
    ``user_decision`` the presence of ``reason``/``reason_text`` upgrades it to
    Tier 2. Returns the row id, or ``None`` if the event already existed
    (idempotent) or anything went wrong (logged, never propagated).
    """
    try:
        if signal_type not in KNOWN_SIGNAL_TYPES:
            # Forward-compatible: record at coverage tier, but flag the unknown
            # so the vocabulary can be extended deliberately, not by accident.
            logger.debug("outcome bus: unknown signal_type %r — recording at T3", signal_type)

        if signal_tier is None:
            has_rationale = bool(reason or reason_text)
            signal_tier = default_tier(signal_type, has_rationale=has_rationale)

        return await _crud.record(
            db,
            source=source,
            ref_type=ref_type,
            ref_id=ref_id,
            signal_type=signal_type,
            signal_tier=signal_tier,
            domain=domain,
            signal_class=signal_class,
            polarity=polarity,
            value=value,
            stated_confidence=stated_confidence,
            prediction_error=prediction_error,
            reason=reason,
            reason_text=reason_text,
            metadata=metadata,
            harvested_from=harvested_from,
            occurred_at=occurred_at,
        )
    except Exception:
        logger.debug(
            "outcome bus: record_outcome failed (%s/%s/%s/%s)",
            source, ref_type, ref_id, signal_type, exc_info=True,
        )
        return None
