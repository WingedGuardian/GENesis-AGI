"""Skill-edit gate control surface — live-read levers.

The ONE place the skill-evolution gates consult for policy (ws2_ledger_config /
ledger_shadow_config lineage). Two levers, both re-read from the merged YAML
(``config/skill_evolution_gate.yaml`` + the user overlay
``~/.genesis/config/skill_evolution_gate.local.yaml``) on EVERY skill-evolution
pass — no boot cache, flip without a restart:

- :func:`skill_gate_mode` — ``off | shadow`` for the static diff-screen Critic
  that screens a proposed SKILL.md edit for self-modification pathologies.
- :func:`skill_replay_mode` / :func:`skill_replay_config` — ``off | shadow`` +
  statistical knobs for the held-out REPLAY gate (WS1), which screens an edit by
  replaying a frozen golden task suite against OLD vs NEW content.

Shadow is the DEFAULT everywhere and an INVALID value degrades to shadow — the
gates are log-only (they never block an edit), so shadow is the safe, observable
baseline (the same fail-safe posture as ledger_shadow_config). ``off`` disables
a gate entirely. There is no ``enforce`` mode yet.

The env kill ``GENESIS_SKILL_EVOLUTION_GATE_OFF`` (genesis.env.skill_gate_off)
is a separate hard override checked first by the gates — it forces off without
touching this config, and covers BOTH gates.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only.
``genesis.mcp.health.settings`` imports MODES/REPLAY_MODES from here, never the
reverse. Accessors return plain values (no eval/skill_replay import) so this
module stays dependency-light.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)

MODES = ("off", "shadow")
REPLAY_MODES = ("off", "shadow")

_CONFIG_NAME = "skill_evolution_gate.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "shadow",  # the static diff-screen Critic; shadow-first
    # Held-out replay gate (WS1). Screens an edit by REPLAYING a per-skill golden
    # suite against OLD vs NEW content and logging a recommend-only verdict. Only
    # fires where a suite exists; heavy (spawns CC sessions), so it runs
    # out-of-band, never in the auto-apply hot path.
    "replay": {"mode": "shadow", "epsilon": 0.05, "min_pairs": 5},
}


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Deep-merges (defaults ← base yaml ← .local.yaml overlay). Missing or
    corrupt files degrade layer-by-layer toward DEFAULTS.
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        logger.warning("skill_evolution_gate base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("skill_evolution_gate overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def skill_gate_mode() -> str:
    """The static diff-screen Critic mode — read live.

    Master ``enabled: false`` → ``off``. An invalid value degrades to
    ``shadow`` (observe-only; the gate never blocks an edit, so shadow is the
    safe baseline — never a silent loss of the audit trail).
    """
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # A hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
        # That intent is unambiguous — honor it.
        return "off"
    if mode not in MODES:
        logger.warning("skill_evolution_gate has invalid mode %r — degrading to shadow", mode)
        return "shadow"
    return mode


def skill_replay_mode() -> str:
    """The held-out replay gate mode — ``off | shadow``, read live.

    Master ``enabled: false`` → ``off`` (covers both gates). A missing or
    invalid ``replay.mode`` degrades to ``shadow`` (the log-only safe baseline).
    """
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    replay = cfg.get("replay")
    if not isinstance(replay, dict):
        return "shadow"
    mode = replay.get("mode")
    if mode is False:  # YAML-1.1 `mode: off` → boolean False; honor the intent.
        return "off"
    if mode not in REPLAY_MODES:
        logger.warning("skill_evolution_gate.replay invalid mode %r — degrading to shadow", mode)
        return "shadow"
    return mode


def skill_replay_config() -> dict[str, Any]:
    """Replay-gate knobs (mode + statistical thresholds), read live.

    Returns a plain dict — the caller builds the ``SkillReplayConfig`` (keeps
    this module free of an eval/skill_replay import). Malformed numeric knobs
    degrade to defaults; ``min_pairs`` is clamped to >= 1 so a bad value can
    never let a verdict fire on an empty comparison.
    """
    cfg = load_config()
    replay = cfg.get("replay") if isinstance(cfg.get("replay"), dict) else {}

    def _num(key: str, default: float, cast):
        try:
            return cast(replay.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "mode": skill_replay_mode(),
        "epsilon": _num("epsilon", 0.05, float),
        "min_pairs": max(1, _num("min_pairs", 5, int)),
    }
