"""Config lever for the surplus ideation → 'idea' review-lane promotion pass.

WS-M PR-2. Cloned from ``ego.reconcile_config``'s shape: fresh-read-per-call,
master ``enabled`` flag + an env kill switch, degrade-toward-less-authority on
damage.

The promotion pass (``surplus/jobs/gates.py::_run_maintenance_gc``) graduates
pending, unexpired staged brainstorm ideas into the follow_ups ``'idea'`` review
lane, capped per run. Disabling it stops promotion; staged ideas then simply
TTL-decay in surplus_insights (the pre-PR-2 behaviour) — so ``disabled`` is the
safe degrade direction, and both a bad read and the env kill switch resolve
there.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``INT_KNOBS`` from here (never
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

_CONFIG_NAME = "surplus_ideation_promotion.yaml"

_ENV_KILL_SWITCH = "GENESIS_SURPLUS_IDEATION_PROMOTION_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "cap_per_run": 20,
}

# Public: the settings-domain validator imports these to check knobs.
INT_KNOBS = ("cap_per_run",)


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
        logger.warning("surplus_ideation_promotion base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("surplus_ideation_promotion overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def is_enabled() -> bool:
    """Whether the promotion pass runs — read live.

    Env kill switch or ``enabled: false`` → disabled (staged ideas just
    TTL-decay). A corrupt/unreadable ``enabled`` is coerced by ``bool()``;
    the safe direction is disabled, but a truthy default keeps the feature on
    for a healthy install.
    """
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return False
    cfg = load_config()
    # Hand-edited unquoted `enabled: off` parses as YAML-1.1 boolean False.
    return bool(cfg.get("enabled", True))


def cap_per_run() -> int:
    """Max ideas promoted per GC pass — positive-int knob with DEFAULTS fallback
    so config damage never zeroes a limit or crashes the pass."""
    value = load_config().get("cap_per_run")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS["cap_per_run"])
    return value
