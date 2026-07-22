"""PR-watch control surface — live-read enable lever + knobs.

The ONE place the ``surface_pr_updates`` SessionStart hook consults for policy
(``repo_pulse_config`` lineage). Re-read from the merged YAML
(``config/pr_watch.yaml`` + the user overlay
``~/.genesis/config/pr_watch.local.yaml``) on EVERY call — NO boot cache, since
each SessionStart hook invocation is a fresh short-lived process.

The feature is a pure OUTPUT surface (it injects a nudge; it never acts on the
outside world), so a single ``enabled`` master switch is the whole policy — no
off/propose_only/live ladder like the write-capable levers. A missing/corrupt
config degrades to DEFAULTS. The hook-level kill switch is separate and
stdlib-cheap: ``GENESIS_PR_WATCH_DISABLED=1`` short-circuits the hook before it
imports anything (the hook keeps a stdlib-only budget and cannot read YAML).

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports from here, never the reverse (one-way).
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)

_CONFIG_NAME = "pr_watch.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # How far back to look for steward notifications to (re)surface. Bounds the
    # scan; also the sidecar-prune horizon.
    "lookback_days": 30,
    # A surfaced update keeps reappearing at session start until it is this many
    # days old (then it stops nagging). "Surface until you notice it."
    "resurface_days": 10,
    # Max distinct updates rendered on one line; overflow collapses to "+N more".
    "max_surface": 5,
}

_INT_KNOBS = (
    "lookback_days",
    "resurface_days",
    "max_surface",
)


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Deep-merges (defaults <- base yaml <- .local.yaml overlay). Missing or
    corrupt files degrade layer-by-layer toward DEFAULTS.
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = repo_root() / "config" / _CONFIG_NAME
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        logger.warning("pr_watch base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("pr_watch overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def is_enabled(cfg: dict[str, Any] | None = None) -> bool:
    """Master switch, read live. Anything but an explicit false is enabled."""
    if cfg is None:
        cfg = load_config()
    return cfg.get("enabled", True) is not False


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never crashes
    the hook or zeroes a window."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value
