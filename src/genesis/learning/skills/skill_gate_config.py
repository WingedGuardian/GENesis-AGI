"""Skill-edit Critic control surface — live-read gate lever.

The ONE place the skill-edit Critic consults for policy (ws2_ledger_config /
ledger_shadow_config lineage). Exposes a single lever:

- :func:`skill_gate_mode` — ``off | shadow`` for the Critic that screens
  self-proposed SKILL.md edits, re-read from the merged YAML
  (``config/skill_evolution_gate.yaml`` + the user overlay
  ``~/.genesis/config/skill_evolution_gate.local.yaml``) on EVERY skill-
  evolution pass. No boot cache — the operator flips off/shadow without a
  restart.

Shadow is the DEFAULT and an INVALID value degrades to shadow — the gate is
log-only (it never blocks an edit), so shadow is the safe, observable baseline
(the same fail-safe posture as ledger_shadow_config). ``off`` disables the
Critic entirely (no LLM call, no observation). There is no ``enforce`` mode yet.

The env kill ``GENESIS_SKILL_EVOLUTION_GATE_OFF`` (genesis.env.skill_gate_off)
is a separate hard override checked first by the Critic — it forces off without
touching this config.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only.
``genesis.mcp.health.settings`` imports MODES from here, never the reverse.
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

_CONFIG_NAME = "skill_evolution_gate.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "shadow",  # log-only Critic; shadow-first
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
    """The skill-edit Critic mode — read live.

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
