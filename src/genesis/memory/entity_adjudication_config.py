"""Config lever for the entity adjudication drainer.

Cloned from ``session_awareness.repo_pulse_config``: fresh-read-per-call,
degrade-toward-less-authority on damage, MODES tuple + env kill switch.

Failure posture: a missing/corrupt config degrades to DEFAULTS; an invalid
``mode`` degrades to ``propose_only`` (never a silent ``live`` — the shadow
posture is the safe one). The env kill switch
``GENESIS_ENTITY_ADJUDICATION_DISABLED=1`` forces ``off`` regardless of file.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``MODES`` and ``INT_KNOBS``
from here (never the reverse).
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

MODES = ("off", "propose_only", "live")

_CONFIG_NAME = "entity_adjudication.yaml"

_ENV_KILL_SWITCH = "GENESIS_ENTITY_ADJUDICATION_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "propose_only",
    "drain_budget": 20,
    "sweep_enabled": True,
    "sweep_slice_size": 200,
    "sweep_enqueue_cap": 50,
}

# Public: the settings-domain validator imports this (with MODES) to check knobs.
INT_KNOBS = ("drain_budget", "sweep_slice_size", "sweep_enqueue_cap")


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
        logger.warning("entity_adjudication base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("entity_adjudication overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the drainer must run under — read live.

    Env kill switch → ``off``. Master ``enabled: false`` → ``off``. An invalid
    value degrades to ``propose_only`` (observable, no write authority — never a
    silent ``off`` that would hide the feature, never a silent ``live``).
    """
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return "off"
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # Hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
        return "off"
    if mode not in MODES:
        logger.warning("entity_adjudication has invalid mode %r — degrading to propose_only", mode)
        return "propose_only"
    return mode


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never crashes
    the drainer or zeroes a limit."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value
