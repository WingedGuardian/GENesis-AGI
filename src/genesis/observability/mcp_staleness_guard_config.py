"""Config lever for the MCP stale-code guard.

Cloned from ``cc.foreground_reaper_config``: fresh-read-per-call, MODES tuple +
env kill switch, degrade toward the SAFEST authority on damage.

Modes:
  - ``block``  — (default) refuse a guarded (overwrite/refine-class) tool on a
    stale subprocess with a "restart this session" error.
  - ``warn``   — log a loud warning, let the call through.
  - ``off``    — disable the guard.

A missing/corrupt config degrades to DEFAULTS; an invalid ``mode`` degrades to
``block`` — the SAFEST outcome (never silently drop protection against silent
data corruption). The env kill switch ``GENESIS_MCP_STALENESS_GUARD=1`` forces
``off`` (matches the kill-switch convention of the sibling levers).

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``MODES`` from here (never the
reverse).
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

MODES = ("off", "warn", "block")

_CONFIG_NAME = "mcp_staleness_guard.yaml"

_ENV_KILL_SWITCH = "GENESIS_MCP_STALENESS_GUARD"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "block",
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
        logger.warning("mcp_staleness_guard base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("mcp_staleness_guard overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the guard runs under — read live.

    Env kill switch → ``off``. Master ``enabled: false`` → ``off``. An invalid
    value degrades to ``block`` (the safest — never silently drop the guard).
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
        logger.warning("mcp_staleness_guard has invalid mode %r — degrading to block", mode)
        return "block"
    return mode
