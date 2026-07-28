"""Config lever for voice ACT — the s2s model's remember/remind tools.

Clone of ``cc.foreground_reaper_config``: fresh-read-per-call, MODES tuple + env
kill switch, degrade-toward-LESS-authority on damage.

Modes:
  - ``off``  — the remember/remind tools are NOT offered to the s2s model and
    their handlers refuse. ``ask_genesis`` recall is unaffected. This is the
    default: a fresh autonomous write + owner-egress surface ships dark and is
    armed to ``live`` only after live E2E (mirrors session_ledger_shadow /
    skill_evolution_gate shipping shadow-first).
  - ``live`` — the s2s model may call ``remember(fact)`` (store an episodic
    memory) and ``remind(text, when)`` (queue a scheduled owner reminder).

There is no meaningful "observe" for a write surface, so it is a two-state
lever. A missing/corrupt config degrades to DEFAULTS; an invalid ``mode``
degrades to ``off`` (LEAST authority — never silently write on bad config). The
env kill switch ``GENESIS_VOICE_ACT_DISABLED=1`` forces ``off``.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``MODES`` from here (never
the reverse).
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)

MODES = ("off", "live")

_CONFIG_NAME = "voice_act.yaml"

_ENV_KILL_SWITCH = "GENESIS_VOICE_ACT_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "off",
}


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Deep-merges (defaults ← base yaml ← .local.yaml overlay). Missing or corrupt
    files degrade layer-by-layer toward DEFAULTS.
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("voice_act base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("voice_act overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode voice ACT runs under — read live.

    Env kill switch → ``off``. Master ``enabled: false`` → ``off``. An invalid
    value degrades to ``off`` (a write surface must never act on bad config).
    """
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return "off"
    cfg = load_config()
    enabled = cfg.get("enabled", True)
    if not isinstance(enabled, bool) or not enabled:
        # Fail closed: a hand-edited / overlay non-boolean `enabled` (e.g. the
        # YAML string "false", which is truthy) must never leave a write surface
        # on. Only a real boolean True enables.
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # Hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
        return "off"
    if mode not in MODES:
        logger.warning("voice_act has invalid mode %r — degrading to off", mode)
        return "off"
    return mode
