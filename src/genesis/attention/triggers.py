"""Pluggable, pure L1 triggers — the §4 taxonomy as DATA-parametrized predicates.

Each trigger: ``(utt, window, config) -> TriggerHit | None``. Registries group them by
kind (HARD precision-first / SOFT recall-first / SUPPRESSOR veto). The scorer composes
the soft hits; the engine resolves activation. Pure — no genesis deps, no I/O.

PR3a completes the §4 taxonomy (emotional lexicon, conversational-dynamics, sensitive-
topic / low-ASR-confidence / mode-state suppressors). ``unanswered_question`` is
engine-emitted (it needs state — see ``unanswered_question_update``, NOT a registry
trigger); ``unknown_speaker`` is DEFERRED (no resolved per-speaker identity —
``speaker_name`` is a diarization label dropped upstream; §5 has no voiceprint link).
Names here are the STABLE keys used in ``config.weights`` and the shadow log's
``triggers_fired`` — do not rename casually.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from genesis.attention.config import AttentionConfig
from genesis.attention.types import AmbientUtterance, EngineState, TriggerHit, TriggerKind

TriggerFn = Callable[[AmbientUtterance, Sequence[AmbientUtterance], AttentionConfig], "TriggerHit | None"]

# An invite cue that, ALONGSIDE a Genesis alias, marks an explicit "ask Genesis".
_INVITE_CUE = re.compile(r"\b(ask|tell|what (?:does|do|would|should)|let'?s ask|should we ask)\b", re.IGNORECASE)


def _term_present(text: str, terms: Sequence[str]) -> bool:
    """Whole-word, case-insensitive presence of ANY term (word-boundary match)."""
    low = text.lower()
    return any(re.search(rf"\b{re.escape(t.lower())}\b", low) for t in terms if t)


def _bank_match(text: str, patterns: Sequence[re.Pattern]) -> bool:
    return any(p.search(text) for p in patterns)


def _w(config: AttentionConfig, name: str, default: float) -> float:
    return config.weights.get(name, default)


# ── HARD — precision-first, near-certain perk (still flows to the warrant stage) ──

def hard_ambient_name(utt, window, config):
    if config.aliases and _term_present(utt.text, config.aliases):
        return TriggerHit("ambient_name", TriggerKind.HARD)
    return None


def hard_explicit_invite(utt, window, config):
    # a Genesis alias AND an invite cue in the same utterance ("ask Genesis ...").
    # This is the "explicit summons" that beats a suppressor (see scorer).
    if config.aliases and _term_present(utt.text, config.aliases) and _INVITE_CUE.search(utt.text):
        return TriggerHit("explicit_invite", TriggerKind.HARD)
    return None


# ── SOFT — recall-first, weighted (contribution = config weight) ──

def soft_question(utt, window, config):
    pats = config.lexical_patterns.get("question", ())
    if (pats and _bank_match(utt.text, pats)) or "?" in utt.text:
        return TriggerHit("question", TriggerKind.SOFT, _w(config, "question", 0.30))
    return None


def soft_help_seeking(utt, window, config):
    pats = config.lexical_patterns.get("help_seeking", ())
    if pats and _bank_match(utt.text, pats):
        return TriggerHit("help_seeking", TriggerKind.SOFT, _w(config, "help_seeking", 0.35))
    return None


def soft_multi_speaker(utt, window, config):
    # By-window speaker count only (speaker_label clusters are NOT comparable across
    # windows). Multi-speaker RAISES confidence: less likely the user addressing
    # Genesis directly -> more likely a real ambient conversation (§4).
    if utt.speaker_total is not None and utt.speaker_total >= 2:
        return TriggerHit("multi_speaker", TriggerKind.SOFT, _w(config, "multi_speaker", 0.40))
    return None


def soft_is_user(utt, window, config):
    # WEAK on its own — the user may be talking directly to Genesis via STT right now.
    if utt.is_user == 1:
        return TriggerHit("is_user", TriggerKind.SOFT, _w(config, "is_user", 0.10))
    return None


def soft_domain_keyword(utt, window, config):
    low = utt.text.lower()
    if any(kw.lower() in low for kw in config.domain_keywords):
        return TriggerHit("domain_keyword", TriggerKind.SOFT, _w(config, "domain_keyword", 0.30))
    return None


def soft_known_entity(utt, window, config):
    if config.known_entities and _term_present(utt.text, config.known_entities):
        return TriggerHit("known_entity", TriggerKind.SOFT, _w(config, "known_entity", 0.25))
    return None


# ── SOFT (PR3a) — the rest of the §4 taxonomy ──

def _soft_bank(utt, config, name, default):
    """A soft trigger that fires on a ``lexical_patterns`` bank match (the common shape)."""
    pats = config.lexical_patterns.get(name, ())
    if pats and _bank_match(utt.text, pats):
        return TriggerHit(name, TriggerKind.SOFT, _w(config, name, default))
    return None


def soft_decision(utt, window, config):
    return _soft_bank(utt, config, "decision", 0.30)


def soft_task_reminder(utt, window, config):
    return _soft_bank(utt, config, "task_reminder", 0.35)


def soft_temporal_deadline(utt, window, config):
    return _soft_bank(utt, config, "temporal_deadline", 0.25)


def soft_quantity_money(utt, window, config):
    return _soft_bank(utt, config, "quantity_money", 0.25)


def soft_recall_cue(utt, window, config):
    return _soft_bank(utt, config, "recall_cue", 0.35)


def soft_dispute(utt, window, config):
    return _soft_bank(utt, config, "dispute", 0.30)


def soft_emotional(utt, window, config):
    # ONE trigger for the whole emotional lexicon (frustration/excitement/confusion/
    # urgency) — a text proxy for prosody (§4). Fires once if ANY emotional bank matches.
    for pats in config.emotional_patterns.values():
        if pats and _bank_match(utt.text, pats):
            return TriggerHit("emotional", TriggerKind.SOFT, _w(config, "emotional", 0.20))
    return None


def soft_topic_continuation(utt, window, config):
    # a follow-on within an active attention window (§4 conversational dynamics): more than
    # this one utt still in the context window => an ongoing exchange. Pure over ``window``.
    if len(window) > 1:
        return TriggerHit("topic_continuation", TriggerKind.SOFT, _w(config, "topic_continuation", 0.15))
    return None


_STOPWORDS = frozenset({
    "the", "a", "an", "of", "to", "and", "or", "is", "are", "was", "were", "be", "been",
    "do", "did", "i", "you", "we", "it", "that", "this", "in", "on", "at", "for", "with",
    "have", "has", "had", "not", "no", "so", "but", "if", "then", "he", "she", "they",
    "them", "my", "your", "our",
})


def _content_tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9']+", text.lower()) if t not in _STOPWORDS}


def soft_lexical_repetition(utt, window, config):
    # an unresolved repeat within the window (§4): this utt shares >= min_tokens CONTENT
    # tokens (stopwords excluded) with an earlier window utt. Pure over ``window``.
    min_t = config.state_modifiers.lexical_repetition_min_tokens
    cur = _content_tokens(utt.text)
    if len(cur) < min_t:
        return None
    for prev in window[:-1]:          # window[-1] is the current utt (already appended by the engine)
        if len(cur & _content_tokens(prev.text)) >= min_t:
            return TriggerHit("lexical_repetition", TriggerKind.SOFT, _w(config, "lexical_repetition", 0.20))
    return None


def _is_question(utt, config) -> bool:
    """``soft_question``'s predicate, reused by the unanswered-question look-ahead."""
    pats = config.lexical_patterns.get("question", ())
    return bool((pats and _bank_match(utt.text, pats)) or "?" in utt.text)


def unanswered_question_update(state: EngineState, utt, config):
    """Forward-only unanswered-question look-ahead (§4 conversational dynamics). Engine-
    called (it needs state — NOT a registry trigger). ONE pass, no mutate-during-iterate:
    a pending "?" older than ``unanswered_question_s`` EMITS an ``unanswered_question`` soft
    hit; a pending "?" that a later substantive non-question utt answered within the window
    is DROPPED; then this utt is enqueued if it is itself a question. Mutates
    ``state.pending_questions`` in place; returns the emit ``TriggerHit | None``.
    """
    sm = config.state_modifiers
    emitted, answered = set(), set()
    for (qid, qts) in state.pending_questions:
        if utt.ts - qts > sm.unanswered_question_s:
            emitted.add(qid)
        elif utt.id != qid and utt.n_tokens >= 1 and not _is_question(utt, config):
            answered.add(qid)
    state.pending_questions = [
        (qid, qts) for (qid, qts) in state.pending_questions
        if qid not in emitted and qid not in answered
    ]
    if _is_question(utt, config):
        state.pending_questions.append((utt.id, utt.ts))
    if emitted:
        return TriggerHit("unanswered_question", TriggerKind.SOFT, _w(config, "unanswered_question", 0.30))
    return None


# ── SUPPRESSOR — veto (overrides soft fires; only an explicit summons beats it) ──

def suppress_explicit_dismissal(utt, window, config):
    pats = config.suppressor_patterns.get("explicit_dismissal", ())
    if pats and _bank_match(utt.text, pats):
        return TriggerHit("explicit_dismissal", TriggerKind.SUPPRESSOR)
    return None


# ── SUPPRESSOR (PR3a) — sensitive_topic (on, empty bank) + edge-only vetoes (OFF by default) ──

def suppress_sensitive_topic(utt, window, config):
    pats = config.suppressor_patterns.get("sensitive_topics", ())   # existing bank key is plural
    if pats and _bank_match(utt.text, pats):
        return TriggerHit("sensitive_topic", TriggerKind.SUPPRESSOR)
    return None


def suppress_low_asr_confidence(utt, window, config):
    # A hard veto reserved for the catastrophic-ASR tail; OFF by default — capture_clarity
    # already dampens the score by (1 - frac_lt_1), so enabling this DOUBLE-COUNTS. It's an
    # edge-only opt-in for when even a high-relevance window is too garbled to trust.
    if utt.frac_lt_1 >= config.state_modifiers.low_asr_frac_lt_1:
        return TriggerHit("low_asr_confidence", TriggerKind.SUPPRESSOR)
    return None


def _mode_active(utt, mode: str) -> bool:
    return mode in utt.mode_state.split()


def suppress_mode_active_listen(utt, window, config):
    # capture-only Listen Mode ON -> passive must not perk. Inert offline (mode_state="unknown").
    if _mode_active(utt, "listen_active"):
        return TriggerHit("mode_active_listen", TriggerKind.SUPPRESSOR)
    return None


def suppress_mode_active_s2s(utt, window, config):
    # a wake-word S2S turn is in progress -> passive stays quiet.
    if _mode_active(utt, "s2s_active"):
        return TriggerHit("mode_active_s2s", TriggerKind.SUPPRESSOR)
    return None


def suppress_mode_global_mute(utt, window, config):
    if _mode_active(utt, "global_mute"):
        return TriggerHit("mode_global_mute", TriggerKind.SUPPRESSOR)
    return None


HARD_TRIGGERS: dict[str, TriggerFn] = {
    "ambient_name": hard_ambient_name,
    "explicit_invite": hard_explicit_invite,
}

SOFT_TRIGGERS: dict[str, TriggerFn] = {
    "question": soft_question,
    "help_seeking": soft_help_seeking,
    "multi_speaker": soft_multi_speaker,
    "is_user": soft_is_user,
    "domain_keyword": soft_domain_keyword,
    "known_entity": soft_known_entity,
    # ── PR3a: §4 taxonomy completion (unanswered_question is engine-emitted, not here) ──
    "decision": soft_decision,
    "task_reminder": soft_task_reminder,
    "temporal_deadline": soft_temporal_deadline,
    "quantity_money": soft_quantity_money,
    "recall_cue": soft_recall_cue,
    "dispute": soft_dispute,
    "emotional": soft_emotional,
    "topic_continuation": soft_topic_continuation,
    "lexical_repetition": soft_lexical_repetition,
}

SUPPRESSORS: dict[str, TriggerFn] = {
    "explicit_dismissal": suppress_explicit_dismissal,
    # PR3a — registered; only explicit_dismissal + sensitive_topic are ENABLED by default.
    "sensitive_topic": suppress_sensitive_topic,
    "low_asr_confidence": suppress_low_asr_confidence,
    "mode_active_listen": suppress_mode_active_listen,
    "mode_active_s2s": suppress_mode_active_s2s,
    "mode_global_mute": suppress_mode_global_mute,
}
