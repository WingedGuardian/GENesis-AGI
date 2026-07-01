"""Genesis-owned attention config — the trigger banks, weights, and thresholds that
parametrize the deterministic L1 gate. The taxonomy is DATA, not code: the same
versioned JSON (``~/.genesis/config/attention_config.json``) loads identically here
and, later, at the edge. Pure — stdlib only (json/re/pathlib), no genesis deps.

``version`` enforces the calibration model (design §6): behaviour is FIXED within a
deployed version; a re-tune ships a NEW version — never a live jittery threshold.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

_Bank = dict[str, tuple[re.Pattern, ...]]


@dataclass(frozen=True)
class StateModifiers:
    """The state dials that bend the threshold / shape the window (all in seconds
    or unitless multipliers; keyed off event-ts deltas, never wall-clock)."""

    context_window_s: float = 8.0          # rolling context window (SAS §10-E)
    context_cap_s: float = 12.0            # stale past this — drop from window
    session_gap_s: float = 30.0            # silence gap that starts a NEW attention session
    session_stickiness_mult: float = 1.3   # in-session soft-score multiplier (stickiness)
    cooldown_s: float = 30.0               # refractory window after a perk
    cooldown_raise: float = 0.2            # threshold ADD while in cooldown (anti-twitch)
    unanswered_question_s: float = 5.0     # a "?" with no reply within this -> a soft signal


@dataclass(frozen=True)
class Thresholds:
    soft_perk: float = 0.6         # effective soft-score >= this -> SOFT activation
    l15_graduation: float = 0.4    # score >= this -> would graduate to L1.5 (stub in v1)


@dataclass(frozen=True)
class AttentionConfig:
    version: str
    aliases: tuple[str, ...]                 # Genesis's own names (ambient-name trigger)
    domain_keywords: tuple[str, ...]         # the user's active projects/topics/tech
    known_entities: tuple[str, ...]          # user_contacts names (+ aliases), flattened
    lexical_patterns: _Bank                  # intent banks (question, help_seeking, ...)
    emotional_patterns: _Bank                # frustration/excitement/confusion/urgency
    suppressor_patterns: _Bank               # explicit_dismissal, sensitive_topics
    weights: dict[str, float]                # soft-trigger name -> weight
    state_modifiers: StateModifiers
    thresholds: Thresholds
    suppressors_enabled: tuple[str, ...]

    @classmethod
    def from_dict(cls, d: dict) -> AttentionConfig:
        def compile_bank(bank: dict | None) -> _Bank:
            return {
                key: tuple(re.compile(p, re.IGNORECASE) for p in (pats or []))
                for key, pats in (bank or {}).items()
            }

        def sub(kls, raw: dict | None):
            raw = raw or {}
            return kls(**{k: raw[k] for k in raw if k in kls.__dataclass_fields__})

        return cls(
            version=str(d.get("version", "0")),
            aliases=tuple(d.get("aliases", [])),
            domain_keywords=tuple(d.get("domain_keywords", [])),
            known_entities=tuple(d.get("known_entities", [])),
            lexical_patterns=compile_bank(d.get("lexical_patterns")),
            emotional_patterns=compile_bank(d.get("emotional_patterns")),
            suppressor_patterns=compile_bank(d.get("suppressor_patterns")),
            weights={k: float(v) for k, v in (d.get("weights") or {}).items()},
            state_modifiers=sub(StateModifiers, d.get("state_modifiers")),
            thresholds=sub(Thresholds, d.get("thresholds")),
            suppressors_enabled=tuple(d.get("suppressors_enabled", [])),
        )


def load_config(path: str | Path) -> AttentionConfig:
    """Load + compile a versioned ``attention_config.json``."""
    return AttentionConfig.from_dict(json.loads(Path(path).expanduser().read_text()))
