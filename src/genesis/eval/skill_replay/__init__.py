"""Skill-replay held-out regression gate (WS1) — SHADOW / recommend-only.

Replays a frozen golden task suite against the OLD vs NEW SKILL.md content and
logs a ``net_positive | regression | inconclusive`` verdict. It never blocks an
edit and mutates no cognition — the execution complement to the static
``skill_edit_critic`` diff-screen.
"""

from __future__ import annotations

from genesis.eval.skill_replay.types import (
    VERDICT_INCONCLUSIVE,
    VERDICT_NET_POSITIVE,
    VERDICT_REGRESSION,
    SkillReplayConfig,
    SkillReplayVerdict,
)
from genesis.eval.skill_replay.verdict import compute_verdict

__all__ = [
    "VERDICT_INCONCLUSIVE",
    "VERDICT_NET_POSITIVE",
    "VERDICT_REGRESSION",
    "SkillReplayConfig",
    "SkillReplayVerdict",
    "compute_verdict",
]
