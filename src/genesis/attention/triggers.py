"""Pluggable, pure L1 triggers — the §4 taxonomy as DATA-parametrized predicates.

Each trigger: ``(utt, window, config) -> TriggerHit | None``. Registries group them by
kind (HARD precision-first / SOFT recall-first / SUPPRESSOR veto). The scorer composes
the soft hits; the engine resolves activation. Pure — no genesis deps, no I/O.

PR1 ships a high-signal subset; the remaining §4 triggers (emotional lexicon,
unknown-speaker, unanswered-question, sensitive-topic, low-ASR-confidence, mode-state
suppressors) land in PR3. Names here are the STABLE keys used in ``config.weights`` and
in the shadow log's ``triggers_fired`` — do not rename casually.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Sequence

from genesis.attention.config import AttentionConfig
from genesis.attention.types import AmbientUtterance, TriggerHit, TriggerKind

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


# ── SUPPRESSOR — veto (overrides soft fires; only an explicit summons beats it) ──

def suppress_explicit_dismissal(utt, window, config):
    pats = config.suppressor_patterns.get("explicit_dismissal", ())
    if pats and _bank_match(utt.text, pats):
        return TriggerHit("explicit_dismissal", TriggerKind.SUPPRESSOR)
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
}

SUPPRESSORS: dict[str, TriggerFn] = {
    "explicit_dismissal": suppress_explicit_dismissal,
}
