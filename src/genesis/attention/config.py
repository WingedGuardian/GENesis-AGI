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

    context_window_s: float = 8.0          # RESERVED (PR3): the SAS 8s active-signal window
    context_cap_s: float = 12.0            # v1 window-eviction boundary — drop utterances older than this
    session_gap_s: float = 30.0            # silence gap that starts a NEW attention session
    session_stickiness_mult: float = 1.3   # in-session soft-score multiplier (stickiness)
    cooldown_s: float = 30.0               # refractory window after a perk
    cooldown_raise: float = 0.2            # threshold ADD while in cooldown (anti-twitch)
    unanswered_question_s: float = 5.0     # a "?" with no reply within this -> a soft signal
    # ── PR3a: decay + suppressor dials ──
    decay_window_s: float = 30.0           # in-session stickiness fully decays to the floor by this off-topic gap (§9: 5-30s)
    decay_floor_mult: float = 1.0          # stickiness decays TOWARD this — removes the bonus, never a below-baseline penalty
    low_asr_frac_lt_1: float = 0.35        # frac_lt_1 >= this -> low_asr_confidence suppressor (OFF by default; capture_clarity already dampens by 1-frac_lt_1)
    lexical_repetition_min_tokens: int = 3  # a "repeat" needs >= this many shared CONTENT tokens (guards stopword noise)


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


# ── default starter config (the ~/.genesis overlay overrides; used by the runner) ──
DEFAULT_CONFIG_PATH = "~/.genesis/config/attention_config.json"


def default_config_dict() -> dict:
    """A complete generic STARTER config for the first shadow pass. ``domain_keywords``
    is a coarse tech-talk starter; ``known_entities`` is empty (a generator fills it
    from ``user_contacts`` later). PR3a wires the full §4 soft taxonomy (all lexical +
    emotional banks) + conversational-dynamics; ``sensitive_topics`` ships empty (inert
    until a config author fills it) and the ``low_asr_confidence`` / ``mode_*`` suppressors
    are REGISTERED but OFF by default (not in ``suppressors_enabled``). All weights/
    thresholds are the calibration surface the shadow review tunes."""
    return {
        "version": "0.1.0-default",
        "aliases": ["genesis"],
        "domain_keywords": [
            "genesis", "routing", "memory", "embedding", "retrieval", "attention",
            "ambient", "voice", "ego", "autonomy", "reflection", "dashboard",
            "telegram", "qdrant", "sqlite", "migration", "outreach", "procedure",
            "deliberate", "omnipresence", "pipeline", "deploy", "model", "prompt",
            "agent", "worktree", "backup",
        ],
        "known_entities": [],
        "lexical_patterns": {
            "question": [
                r"\b(how|what|why|when|where|who|which)\b.{0,40}\?",
                r"\b(should|could|can|do|does|did|is|are|will|would)\s+(i|we|you|it|they)\b",
            ],
            "help_seeking": [
                r"\bhow do i\b", r"\bi'?m stuck\b", r"\bcan'?t figure\b",
                r"\bnot sure how\b", r"\bhelp me\b",
            ],
            "decision": [r"\bwe should\b", r"\blet'?s\b", r"\bi'?ll\b", r"\bwe need to\b"],
            "task_reminder": [r"\bremind me\b", r"\blook up\b", r"\bfind out\b", r"\bdon'?t forget\b"],
            "temporal_deadline": [r"\btomorrow\b", r"\bdeadline\b", r"\btonight\b", r"\bnext week\b"],
            "quantity_money": [r"\$\s?\d", r"\bdollars?\b", r"\bbudget\b"],
            "recall_cue": [r"\bwhat did we say\b", r"\bdidn'?t we\b", r"\bremember when\b"],
            "dispute": [r"\bis it true\b", r"\bactually,? no\b", r"\bare you sure\b"],
        },
        "emotional_patterns": {
            "frustration": [r"\bugh\b", r"\bso annoying\b", r"\bfrustrat", r"\bcan'?t stand\b"],
            "excitement": [r"\bthis is huge\b", r"\bamazing\b", r"\bcan'?t wait\b", r"\bso cool\b"],
            "confusion": [r"\bconfused\b", r"\bi don'?t (get|understand)\b", r"\bmakes no sense\b"],
            "urgency": [r"\bhurry\b", r"\basap\b", r"\bright now\b", r"\burgent\b"],
        },
        "suppressor_patterns": {
            "explicit_dismissal": [
                r"\bnever mind\b", r"\bnot you\b", r"\bforget it\b",
                r"\bjust between us\b", r"\bnone of your\b",
            ],
            "sensitive_topics": [],
        },
        "weights": {
            "question": 0.30, "help_seeking": 0.35, "multi_speaker": 0.40,
            "is_user": 0.10, "domain_keyword": 0.30, "known_entity": 0.25,
            # PR3a — §4 taxonomy completion (SEED weights; PR3c re-tunes against labels).
            # multi_speaker stays 0.40 — DILUTED by these new signals, NOT down-weighted here.
            "decision": 0.30, "task_reminder": 0.35, "temporal_deadline": 0.25,
            "quantity_money": 0.25, "recall_cue": 0.35, "dispute": 0.30,
            "emotional": 0.20, "topic_continuation": 0.15, "lexical_repetition": 0.20,
            "unanswered_question": 0.30,
        },
        "state_modifiers": {},   # all StateModifiers defaults
        "thresholds": {},        # all Thresholds defaults (soft_perk 0.6, l15_graduation 0.4)
        # low_asr_confidence + mode_* are registered but deliberately NOT enabled by default
        # (double-count with capture_clarity / inert offline). sensitive_topic is on but empty.
        "suppressors_enabled": ["explicit_dismissal", "sensitive_topic"],
    }
