"""Config lever for the context-injection watcher (awareness hourly band).

Cloned from ``follow_up_watchdog_config``: read-and-alert only, so there is no
authority to grade — an ``enabled`` master switch + int knobs + an env kill
switch.

Failure posture: a missing/corrupt config degrades to DEFAULTS; the env kill
switch ``GENESIS_CONTEXT_INJECTION_WATCH_DISABLED=1`` forces the check off
regardless of the file.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``DEFAULTS`` / ``INT_KNOBS``
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

_CONFIG_NAME = "context_injection_watch.yaml"
_ENV_KILL_SWITCH = "GENESIS_CONTEXT_INJECTION_WATCH_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # How far back a persisted hook-stdout filing counts as a live incident.
    # The check runs hourly; 24h means an overnight filing still pages in the
    # morning rather than aging out silently between ticks.
    "lookback_hours": 24,
    # Cap the filings listed inside a single alert.
    "max_listed": 5,
    # This class ran unnoticed for a MONTH because every layer failed quietly;
    # critical = the ~5-minute Telegram path, not the morning report.
    "alert_priority": "critical",
}

INT_KNOBS = ("lookback_hours", "max_listed")
_VALID_ALERT_PRIORITY = ("low", "medium", "high", "critical")


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
        logger.warning("context_injection_watch base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("context_injection_watch overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def is_enabled() -> bool:
    """True unless the env kill switch is set or the config disables it."""
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return False
    return bool(load_config().get("enabled", True))


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never zeroes a
    limit or crashes the check."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value


def alert_priority(cfg: dict[str, Any]) -> str:
    """The configured alert priority, falling back to the default on damage."""
    value = cfg.get("alert_priority")
    if value in _VALID_ALERT_PRIORITY:
        return str(value)
    return str(DEFAULTS["alert_priority"])
