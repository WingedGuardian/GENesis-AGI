"""Config lever for the foreground-session liveness reaper (D3).

Cloned from ``cc.rate_limit_resume_config``: fresh-read-per-call, MODES tuple +
env kill switch, degrade-toward-less-authority on damage.

Modes (increasing authority):
  - ``off``     — the reaper does nothing; dark foreground rows stay ``active``
    (the pre-D3 status quo).
  - ``observe`` — reap dark rows to ``checkpointed`` + write dark-session
    observations, but NEVER notify the user (the autonomous-notify kill the
    architecture review asked for).
  - ``notify``  — observe + notify the origin user on the crisp "unanswered
    user turn" signal. The fuzzy promise-regex signal is ALWAYS shadow-logged
    only (never notified) until its precision is measured, regardless of mode.

Default ``notify``: the 2026-07-20 silent-death's core failure was the user
never being told their request died. A missing/corrupt config degrades to
DEFAULTS; an invalid ``mode`` degrades to ``observe`` (reap safely — never a
silent unattended notify, never a silent ``off`` that hides the feature). The
env kill switch ``GENESIS_FOREGROUND_REAPER_DISABLED=1`` forces ``off``.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``MODES`` / ``INT_KNOBS``
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

MODES = ("off", "observe", "notify")

_CONFIG_NAME = "cc_foreground_reaper.yaml"

_ENV_KILL_SWITCH = "GENESIS_FOREGROUND_REAPER_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "notify",
    # A foreground row idle this long with no live turn is treated as dark.
    # Generous: legitimate turns keep last_activity_at fresh via update_activity,
    # so 24h of true silence means abandoned, not thinking.
    "idle_hours": 24,
    # Loud-truncation cap: dark rows processed per reaper pass.
    "max_per_tick": 200,
}

# Public: the settings-domain validator imports these to check knobs.
INT_KNOBS = (
    "idle_hours",
    "max_per_tick",
)


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
        logger.warning("cc_foreground_reaper base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("cc_foreground_reaper overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the reaper runs under — read live.

    Env kill switch → ``off``. Master ``enabled: false`` → ``off``. An invalid
    value degrades to ``observe`` (reap safely, no unattended notify — never a
    silent ``off`` that hides the feature, never a silent ``notify``).
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
        logger.warning("cc_foreground_reaper has invalid mode %r — degrading to observe", mode)
        return "observe"
    return mode


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never zeroes a
    limit or crashes the reaper."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value
