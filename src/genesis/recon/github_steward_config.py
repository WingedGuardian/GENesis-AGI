"""Config lever for the GitHub account-activity monitor (the "github steward").

Cloned from ``cc.foreground_reaper_config``: fresh-read-per-call, ``MODES``
tuple + env kill switch, degrade-toward-less-authority on damage.

Modes (increasing authority):
  - ``off``     — the monitor does not run.
  - ``observe`` — poll + record observations + seed the seen-actor set, but
    NEVER ping. First-deploy default: prevents a cold-start ping-storm (every
    existing contributor would otherwise classify as first-time). The operator
    flips to ``live`` once the seen-actor set is seeded.
  - ``live``    — observe + push priority Telegram pings for genuine external
    activity (first-time contributor, security advisory, account notice).

A missing/corrupt config degrades to DEFAULTS; an invalid ``mode`` degrades to
``observe`` (safe: record but never ping — never a silent ``off`` that hides the
feature, never an unattended ``live``). Env kill switch
``GENESIS_GITHUB_STEWARD_DISABLED=1`` forces ``off``.

Generalizability: NO install-specific repo names ship here. ``flagship_repos``
defaults to empty; each install pins its own via the gitignored
``github_steward.local.yaml`` overlay, and an empty list makes the monitor
resolve the owner's active public repos live (capped).

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only.
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

MODES = ("off", "observe", "live")

_CONFIG_NAME = "github_steward.yaml"

_ENV_KILL_SWITCH = "GENESIS_GITHUB_STEWARD_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # First deploy: observe (seed seen-actors, no pings) until the operator
    # flips to live. Never default to a ping-capable mode on a fresh install.
    "mode": "observe",
    # Repos deep-polled for full actor-bearing activity. EMPTY in the shipped
    # default (no install-specific names). Empty → resolve live: the owner's
    # public, non-fork repos pushed within `auto_select_days`, capped at
    # `auto_select_cap`. Pin specific repos via the .local.yaml overlay.
    "flagship_repos": [],
    "auto_select_cap": 5,
    "auto_select_days": 60,
    # Logins to always treat as automation even though they're plain User/Org
    # accounts (e.g. a review bot posting under a user account). Config lever,
    # not a hardcoded literal.
    "automation_denylist": [],
    # Loud-truncation cap: activity events processed per tick.
    "max_events_per_tick": 100,
}

# Public: the settings-domain validator imports these to check knobs.
INT_KNOBS = ("auto_select_cap", "auto_select_days", "max_events_per_tick")


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
        logger.warning("github_steward base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("github_steward overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the monitor runs under — read live.

    Env kill switch → ``off``. Master ``enabled: false`` → ``off``. An invalid
    value degrades to ``observe`` (record safely, never ping).
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
        logger.warning("github_steward has invalid mode %r — degrading to observe", mode)
        return "observe"
    return mode


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never zeroes a
    limit or crashes the monitor."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value


def str_list(cfg: dict[str, Any], key: str) -> list[str]:
    """A list-of-strings knob, damage-tolerant (non-list / non-str → ignored)."""
    value = cfg.get(key)
    if not isinstance(value, list):
        return []
    return [v for v in value if isinstance(v, str) and v.strip()]
