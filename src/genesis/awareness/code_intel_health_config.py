"""Config lever for the code-intel health check (awareness hourly band).

Cloned from ``context_injection_watch_config``: read-and-alert only, so there is
no authority to grade — an ``enabled`` master switch + knobs + an env kill
switch.

Failure posture: a missing/corrupt config degrades to DEFAULTS; the env kill
switch ``GENESIS_CODE_INTEL_HEALTH_DISABLED=1`` forces the check off regardless
of the file.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``DEFAULTS`` from here (never
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

_CONFIG_NAME = "code_intel_health.yaml"
_ENV_KILL_SWITCH = "GENESIS_CODE_INTEL_HEALTH_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # WHICH path code-intel is expected to have an index for. Empty = repo root,
    # which is what EVERY marker writer currently hardcodes (the post-commit
    # hook, disk_reclaim, the gitnexus surplus job) — so the default is the
    # correct value on a stock install and this knob should usually stay empty.
    #
    # It exists because the index's on-disk name is derived from the FULL path,
    # so an install that re-points its indexer at a subdirectory needs the check
    # to follow. It steers ONLY THIS CHECK: nothing in the indexing path reads
    # this file. Setting it without also re-pointing the writers makes the check
    # look for an index nothing will ever build — reporting ABSENT forever,
    # which is the permanently-wrong alarm this knob was meant to avoid.
    "indexed_path": "",
    # A dead code index degrades tool QUALITY; it does not lose the user's data
    # or their session context. `high` reaches the morning report; `critical` is
    # the ~5-minute Telegram path and is reserved for the context-loss class.
    "alert_priority": "high",
}

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
        logger.warning("code_intel_health base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("code_intel_health overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def is_enabled() -> bool:
    """True unless the env kill switch is set or the config EXPLICITLY disables it.

    Only a real boolean turns the check off. Plain truthiness would let
    ``enabled: null`` / ``0`` / ``[]`` — a damaged base file or a half-written
    overlay — silence the only alarm covering code-intel, which is the same
    fail-open shape as the two-week silence this check exists to end. Config
    damage therefore degrades toward WATCHING.
    """
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return False
    value = load_config().get("enabled", True)
    return value if isinstance(value, bool) else True


def indexed_path(cfg: dict[str, Any]) -> Path:
    """The path code-intel is expected to have indexed; repo root by default.

    Resolved, not merely expanded: the indexer realpath's the repo path it
    records (``index_marker.canonical_repo``), so a config value crossing a
    symlink or containing ``..`` would slug differently and silently match no
    marker at all — dropping every euthanized request for the target AND
    leaving ``requested`` False, so an absent index would read as a fresh
    install. Both halves of the check would go quiet together.
    """
    value = cfg.get("indexed_path")
    if isinstance(value, str) and value.strip():
        raw = Path(value.strip()).expanduser()
    else:
        raw = repo_root()
    try:
        return raw.resolve()
    except OSError:
        return raw


def alert_priority(cfg: dict[str, Any]) -> str:
    """The configured alert priority, falling back to the default on damage."""
    value = cfg.get("alert_priority")
    if value in _VALID_ALERT_PRIORITY:
        return str(value)
    return str(DEFAULTS["alert_priority"])
