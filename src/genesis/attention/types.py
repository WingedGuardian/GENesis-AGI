"""Data types for the passive-listening attention engine (v1, shadow mode).

Pure — NO genesis-runtime imports, NO I/O (enforced by the edge-port test). All
time-state is keyed off the utterance ``ts`` (epoch seconds), never wall-clock, so an
offline batch replay over a snapshot is byte-identical to a future live edge run.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum


class Activation(StrEnum):
    """The gate's verdict for an utterance."""

    HARD = "hard"              # near-certain perk (a precision-first trigger fired)
    SOFT = "soft"              # weighted soft-score crossed the perk threshold
    SUPPRESSED = "suppressed"  # a suppressor vetoed an otherwise-firing event


class TriggerKind(StrEnum):
    HARD = "hard"
    SOFT = "soft"
    SUPPRESSOR = "suppressor"


@dataclass(frozen=True)
class AmbientUtterance:
    """One normalized, source-agnostic utterance the engine consumes.

    Populated by an ADAPTER (``SnapshotSource`` offline; a ``LiveBridgeSource`` at
    the edge later) — the pure core never opens ``ambient.db``. ``text`` is used only
    by lexical triggers and lives in memory only; it is NEVER persisted (firewall).
    """

    id: int                     # source row id (used for window_ref — never text)
    ts: float                   # epoch seconds, utterance END — the ONE clock
    text: str                   # transcript; lexical-trigger input only, never stored
    duration_s: float
    is_user: int | None         # 1=user / 0=other / None=no verdict (conservative)
    speaker_total: int | None   # # speakers in THIS utt's diar window (wN:c/TOTAL); None if unlabeled
    n_tokens: int
    frac_lt_1: float            # fraction of ys_log_probs < -1.0 (ASR-confidence stat)
    rms: float
    mode_state: str = "unknown"  # "unknown" offline; live interaction-plane booleans at the edge
    source: str = ""            # connection/device id


@dataclass(frozen=True)
class TriggerHit:
    """One fired trigger. ``contribution`` is its weighted add to the soft score
    (0.0 for hard flags and suppressors — they act on activation, not the score)."""

    name: str
    kind: TriggerKind
    contribution: float = 0.0


@dataclass(frozen=True)
class WindowRef:
    """Reference to the ambient rows behind an event — IDs + ts range ONLY, never
    text (the firewall: raw transcript lives only in the transient snapshot file).
    ``snapshot_id`` is attached by the consumer at persist time, not the core."""

    session_id: str
    utt_ids: tuple[int, ...]
    ts_start: float
    ts_end: float


@dataclass(frozen=True)
class AttentionEvent:
    """What the gate WOULD have perked up on. Snapshot-agnostic (the pure-core
    output); the consumer stamps snapshot_id/config_version when persisting."""

    activation: Activation
    score: float
    triggers_fired: tuple[TriggerHit, ...]
    suppressors: tuple[str, ...]
    session_id: str
    window_ref: WindowRef
    ts: float
    mode_state: str
    clarity: float                 # capture_clarity of the triggering utterance
    l15_verdict: dict | None = None  # {real, perk} from L1.5 — always None in v1 (stub)


@dataclass
class EngineState:
    """Rolling engine state, passed explicitly through the fold (NO module-level
    state — cf. the awareness scorer anti-pattern). Mutated in place and returned;
    serializable via ``dataclasses.asdict`` for the future edge runner.

    ``window`` holds recent utterances within the context window (transient, text
    included — in memory only). ``last_perk_ts`` drives the cooldown; it intentionally
    persists across sessions (anti-twitch). (PR3 adds ``pending_questions`` for the
    unanswered-question bounded-lookahead signal — out of the PR1 trigger subset.)
    """

    session_id: str | None = None
    session_started_ts: float | None = None
    last_utt_ts: float | None = None
    last_perk_ts: float | None = None
    window: deque = field(default_factory=deque)
