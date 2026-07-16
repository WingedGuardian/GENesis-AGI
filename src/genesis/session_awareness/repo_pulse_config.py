"""Repo-pulse control surface — live-read mode lever + knobs.

The ONE place the repo-pulse worker consults for policy (session-manager
PR-4a, ledger_shadow_config lineage):

- :func:`effective_mode` — ``off | propose_only | live``, re-read from the
  merged YAML (``config/repo_pulse.yaml`` + the user overlay
  ``~/.genesis/config/repo_pulse.local.yaml``) on EVERY call. No boot cache
  — each SessionStart-spawned worker is a fresh process anyway.

Unlike the ledger-shadow lever, ``live`` is the DEFAULT here: the fuzzy
tier is proposal-only BY CONSTRUCTION in every mode (the judge's matches
are stored as proposals, never ledger writes), so the lever only gates the
exact tier's marker-triggered auto-absorb — deterministic, evidence-carrying,
and reversible via ``session_ledger_update``. ``propose_only`` is the
de-escalation lever: exact marker hits are recorded as proposals instead of
absorbed. An INVALID mode degrades to ``propose_only`` — toward LESS write
authority, never silently off (a dead pulse would hide rot in the ledger).

Failure posture: a missing/corrupt config degrades to DEFAULTS. The
hook-level kill switch is separate and stdlib-cheap:
``GENESIS_REPO_PULSE_DISABLED=1`` stops the SessionStart hook from even
spawning the worker (the hook cannot read YAML — stdlib-only budget).

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay
only; ``genesis.mcp.health.settings`` imports MODES from here, never the
reverse (one-way, the immunity rule).
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

MODES = ("off", "propose_only", "live")

_CONFIG_NAME = "repo_pulse.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "live",
    "min_interval_minutes": 30,  # global debounce between runs
    "lookback_days": 7,  # first-run / cursor-less enumeration window
    "max_prs": 200,  # gh --limit; n==limit records a loud 'limit_hit'
    "max_items": 40,  # open ledger rows fed to the fuzzy judge
    "max_proposals_per_run": 10,  # fuzzy annotations stored per run
    "inject_confidence_floor": 0.7,  # proposals below this never surface
}

_INT_KNOBS = (
    "min_interval_minutes",
    "lookback_days",
    "max_prs",
    "max_items",
    "max_proposals_per_run",
)


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
        logger.warning("repo_pulse base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("repo_pulse overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the worker must run under — read live.

    Master ``enabled: false`` → ``off``. An invalid value degrades to
    ``propose_only`` (observable, less write authority — never a silent
    off, never a silent absorb).
    """
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # A hand-edited unquoted `mode: off` parses as YAML-1.1 boolean
        # False. That intent is unambiguous — honor it.
        return "off"
    if mode not in MODES:
        logger.warning("repo_pulse has invalid mode %r — degrading to propose_only", mode)
        return "propose_only"
    return mode


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never crashes
    the worker or zeroes a limit."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value


def knob_float01(cfg: dict[str, Any], key: str) -> float:
    """[0,1] float knob with DEFAULTS fallback."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return float(DEFAULTS[key])
    return float(value)
