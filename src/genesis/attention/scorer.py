"""Pure score composition + activation resolution (design §4). No genesis deps, no I/O.

The dial is a single threshold (reply-everything <-> reply-nothing). Soft hits sum to a
relevance, scaled by the utterance's capture-clarity (tolerate garble; weight it down,
never drop it) and by in-session stickiness; state modifiers bend the threshold.
"""
from __future__ import annotations

from collections.abc import Sequence

from genesis.attention.types import Activation, TriggerHit


def soft_relevance(hits: Sequence[TriggerHit]) -> float:
    """Sum of soft-hit weighted contributions."""
    return sum(h.contribution for h in hits)


def resolve_activation(
    *,
    hard_hits: Sequence[TriggerHit],
    suppressor_hits: Sequence[TriggerHit],
    effective: float,
    threshold: float,
) -> Activation | None:
    """The gate verdict, or None if nothing would fire.

    Precedence: a suppressor VETOES an otherwise-firing event -> SUPPRESSED, UNLESS an
    explicit summons (``explicit_invite``) is present (only a summons beats a suppressor;
    a bare name-mention does not). Otherwise: any hard trigger -> HARD; effective score
    >= threshold -> SOFT. A suppressor with nothing to suppress emits no event.
    """
    has_summons = any(h.name == "explicit_invite" for h in hard_hits)
    if suppressor_hits and not has_summons:
        would_fire = bool(hard_hits) or effective >= threshold
        return Activation.SUPPRESSED if would_fire else None
    if hard_hits:
        return Activation.HARD
    if effective >= threshold:
        return Activation.SOFT
    return None
