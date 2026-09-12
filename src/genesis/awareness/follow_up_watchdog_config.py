"""Config lever for the follow-up hygiene watchdog (awareness hourly band).

Cloned from ``cc.rate_limit_resume_config`` minus the mode ladder: this check is
read-and-alert only (it mutates nothing), so there is no authority to grade —
just an ``enabled`` master switch + int knobs + an env kill switch.

Failure posture: a missing/corrupt config degrades to DEFAULTS; the env kill
switch ``GENESIS_FOLLOW_UP_WATCHDOG_DISABLED=1`` forces the check off regardless
of the file.

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

_CONFIG_NAME = "follow_up_watchdog.yaml"
_ENV_KILL_SWITCH = "GENESIS_FOLLOW_UP_WATCHDOG_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # A row must be at least this old (created_at) before an orphaned_scheduled
    # finding fires, and scheduled_at must be this far past for past_due — avoids
    # alerting on a row that a concurrent writer is mid-transition on.
    "grace_hours": 6,
    # Cap the offender ids listed inside a single alert (the alert is one deduped
    # observation; the list is for the morning session to act on).
    "max_listed": 10,
    # Base severity of the alert. Escalated to 'critical' when any flagged row is
    # itself priority='critical'.
    "alert_priority": "high",
}

INT_KNOBS = ("grace_hours", "max_listed")
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
        logger.warning("follow_up_watchdog base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("follow_up_watchdog overlay merge failed", exc_info=True)
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
    """The configured base alert priority, falling back to the default on damage."""
    value = cfg.get("alert_priority")
    if value in _VALID_ALERT_PRIORITY:
        return str(value)
    return str(DEFAULTS["alert_priority"])
