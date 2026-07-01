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


def stickiness_multiplier(
    base: float, off_topic_s: float | None, decay_window_s: float, floor: float = 1.0,
) -> float:
    """In-session stickiness that DECAYS as the conversation drifts off-topic (§4/§9).

    ``off_topic_s`` = ``utt.ts - last_relevance_ts`` (seconds since the last utterance
    that carried ANY soft relevance). Returns ``base`` when fully on-topic (0s) and
    linearly decays toward ``floor``, reaching ``floor`` at ``decay_window_s``; clamped —
    it removes the bonus, never penalizes below ``floor``.

    ``off_topic_s is None`` (first utt of a session — no prior relevant utt yet) or a
    non-positive ``decay_window_s`` -> ``floor`` (no bonus, no crash). Returning ``floor``
    on the first utt matches the pre-PR3a "first-utt gets no stickiness" behaviour.
    """
    if off_topic_s is None or decay_window_s <= 0.0:
        return floor
    frac = min(max(off_topic_s, 0.0) / decay_window_s, 1.0)
    return base - (base - floor) * frac


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
