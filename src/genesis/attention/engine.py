"""The pure attention-engine fold: ``evaluate(utt, state, config) -> (state, event?)``.

Deterministic, event-``ts``-driven (NO wall-clock), NO I/O, NO genesis deps. Offline
batch = ``reduce(evaluate, ts_ordered_utts, EngineState())``; the SAME core runs live at
the edge later behind a different source/sink, no logic change. Enforced by
``tests/test_attention/test_edge_portability.py``.
"""
from __future__ import annotations

from genesis.attention.clarity import capture_clarity, is_blip
from genesis.attention.config import AttentionConfig
from genesis.attention.scorer import resolve_activation, soft_relevance, stickiness_multiplier
from genesis.attention.triggers import (
    HARD_TRIGGERS,
    SOFT_TRIGGERS,
    SUPPRESSORS,
    unanswered_question_update,
)
from genesis.attention.types import (
    Activation,
    AmbientUtterance,
    AttentionEvent,
    EngineState,
    WindowRef,
)


def _run(registry, utt, window, config, *, enabled=None):
    hits = []
    for name, fn in registry.items():
        if enabled is not None and name not in enabled:
            continue
        hit = fn(utt, window, config)
        if hit is not None:
            hits.append(hit)
    return hits


def evaluate(
    utt: AmbientUtterance, state: EngineState, config: AttentionConfig,
) -> tuple[EngineState, AttentionEvent | None]:
    """One fold step. Mutates ``state`` in place and returns ``(state, event | None)``."""
    sm = config.state_modifiers
    clarity = capture_clarity(
        utt.rms, utt.duration_s, utt.frac_lt_1, utt.n_tokens, has_audio=utt.has_audio,
    )

    # near-silence junk: ignore entirely (never pollutes windows or timing).
    if is_blip(utt.rms, utt.duration_s, utt.n_tokens, has_audio=utt.has_audio):
        return state, None

    # ── sessionize (gap-based, a pure fold over ts) ──
    new_session = state.last_utt_ts is None or (utt.ts - state.last_utt_ts) > sm.session_gap_s
    if new_session:
        state.session_id = f"s{utt.id}"
        state.session_started_ts = utt.ts
        state.window.clear()
        state.pending_questions = []      # cross-session questions are stale
        state.last_relevance_ts = None    # reset the decay clock (cooldown below persists)
        # cooldown (last_perk_ts) intentionally persists across sessions (anti-twitch).

    # ── context window (evict utterances older than the cap) ──
    state.window.append(utt)
    while state.window and (utt.ts - state.window[0].ts) > sm.context_cap_s:
        state.window.popleft()
    window = list(state.window)

    # ── triggers ──
    hard = _run(HARD_TRIGGERS, utt, window, config)
    soft = _run(SOFT_TRIGGERS, utt, window, config)
    # unanswered-question look-ahead is engine-emitted (it needs state) and also advances
    # the pending-question queue for future utterances.
    uq_hit = unanswered_question_update(state, utt, config)
    if uq_hit is not None:
        soft.append(uq_hit)
    # suppressors_enabled is an ALLOWLIST: empty tuple -> no suppressors run (NOT "all").
    # Pass it straight through (no `or None`, which would misread [] as "run everything").
    supp = _run(SUPPRESSORS, utt, window, config, enabled=config.suppressors_enabled)

    # ── score ──
    relevance = soft_relevance(soft)
    effective = relevance * clarity
    # in-session stickiness that DECAYS as the conversation drifts off-topic (§4). off_topic_s
    # = seconds since the last utt that carried ANY relevance; None on the first relevant utt of
    # a session -> floor (no bonus), matching the pre-PR3a first-utt behaviour without a crash.
    off_topic_s = (utt.ts - state.last_relevance_ts) if state.last_relevance_ts is not None else None
    effective *= stickiness_multiplier(
        sm.session_stickiness_mult, off_topic_s, sm.decay_window_s, sm.decay_floor_mult,
    )
    if relevance > 0.0:
        state.last_relevance_ts = utt.ts   # advance the decay clock on an on-topic utt

    in_cooldown = state.last_perk_ts is not None and (utt.ts - state.last_perk_ts) < sm.cooldown_s
    threshold = config.thresholds.soft_perk + (sm.cooldown_raise if in_cooldown else 0.0)

    activation = resolve_activation(
        hard_hits=hard, suppressor_hits=supp, effective=effective, threshold=threshold,
    )

    state.last_utt_ts = utt.ts
    if activation is None:
        return state, None

    # a real perk (not a suppressed veto) arms the cooldown.
    if activation in (Activation.HARD, Activation.SOFT):
        state.last_perk_ts = utt.ts

    session_id = state.session_id or f"s{utt.id}"
    event = AttentionEvent(
        activation=activation,
        score=round(effective, 4),
        triggers_fired=tuple(hard + soft),
        suppressors=tuple(h.name for h in supp),
        session_id=session_id,
        window_ref=WindowRef(
            session_id=session_id,
            utt_ids=tuple(w.id for w in window),
            ts_start=window[0].ts,
            ts_end=utt.ts,
        ),
        ts=utt.ts,
        mode_state=utt.mode_state,
        clarity=round(clarity, 4),
    )
    return state, event
