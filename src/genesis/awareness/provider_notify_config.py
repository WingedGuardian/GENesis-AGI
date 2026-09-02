"""Config lever for the dead-provider notification sweep (awareness 5-min band).

Cloned from ``follow_up_watchdog_config``, PLUS the mode ladder that check does
not need: this one WRITES (a ``priority="critical"`` observation that becomes a
Telegram), so its authority is graded —

  * ``off``          — the sweep does not run; any open notify rows are resolved
                       so disabling never strands an alert (loop contract).
  * ``propose_only`` — the sweep writes at ``priority="high"``: visible on the
                       dashboard and to the ego, never sent to Telegram
                       (``_critical_observations_job`` polls only critical).
  * ``live``         — the sweep writes at ``priority="critical"``: the one
                       Telegram. The DEFAULT: the feature exists to tell the
                       user, and shipping it dark reproduces the
                       shipped-green-but-inert failure this repo has recorded.

Failure posture: a missing/corrupt config degrades to DEFAULTS; an INVALID mode
degrades to ``propose_only`` (toward LESS write authority, per the settings-lever
rule); the env kill switch ``GENESIS_PROVIDER_NOTIFY_DISABLED=1`` forces ``off``
regardless of the file.

USER-DECIDED (2026-09-02): turning the lever off RESOLVES open notify rows, so
off→on RE-NOTIFIES a provider that is still dead. The accepted cost is a repeat
message across an off/on cycle; the alternative strands unresolved criticals
while the feature is off, which is the exact failure the loop contract exists to
prevent.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports ``DEFAULTS`` / ``MODES`` from here
(never the reverse).
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

_CONFIG_NAME = "provider_outage_notify.yaml"
_ENV_KILL_SWITCH = "GENESIS_PROVIDER_NOTIFY_DISABLED"

MODES = ("off", "propose_only", "live")

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "live",
}


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Deep-merges (defaults ← base yaml ← .local.yaml overlay). Missing or corrupt
    files degrade layer-by-layer toward DEFAULTS, and `settings_update` therefore
    takes effect on the next awareness tick with no restart.
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
        logger.warning("provider_outage_notify base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("provider_outage_notify overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the sweep must honour, after every degradation rule.

    Order matters: the env kill switch outranks the file; ``enabled: false``
    reads as ``off``; YAML-1.1 parses a bare ``mode: off`` as boolean False, so
    that shape is honoured as ``off`` rather than rejected; any OTHER invalid
    value degrades to ``propose_only`` — less write authority, never more.
    """
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return "off"
    cfg = load_config()
    enabled = cfg.get("enabled", True)
    if not isinstance(enabled, bool):
        # A hand-edited `enabled: 'false'` (string) is truthy, so a plain
        # `if not enabled` would read the most disabling-looking value as LIVE —
        # more authority from an invalid value, the exact inversion the file's
        # own contract forbids. Non-bool degrades to propose_only instead.
        logger.warning(
            "provider_outage_notify has non-boolean enabled=%r — degrading to "
            "propose_only",
            enabled,
        )
        return "propose_only"
    if not enabled:
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        return "off"
    if mode not in MODES:
        logger.warning(
            "provider_outage_notify has invalid mode %r — degrading to propose_only",
            mode,
        )
        return "propose_only"
    return str(mode)
