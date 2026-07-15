"""Ledger shadow extractor control surface — live-read mode lever.

The ONE place the ledger shadow worker consults for policy (session-manager
PR-3, ws3_immunity lineage):

- :func:`effective_mode` — ``off | shadow``, re-read from the merged YAML
  (``config/session_ledger_shadow.yaml`` + the user overlay
  ``~/.genesis/config/session_ledger_shadow.local.yaml``) on EVERY call.
  No boot cache — a ``settings_update`` or hand edit takes effect on the
  next PreCompact-spawned worker instantly (each run is a fresh process
  anyway). ``live`` is a RESERVED value: the write path lands with the
  data-gated flip PR, so until then it is coerced to ``shadow`` with a
  warning (sentinel dispatcher precedent) — never a silent live flip.

Failure posture: a missing/corrupt config degrades to DEFAULTS (enabled,
shadow) — shadow writes nothing user-visible, so config damage costs at
most one Haiku call per compaction, never a live-ledger write.

The hook-level kill switch is separate and stdlib-cheap:
``GENESIS_LEDGER_SHADOW_DISABLED=1`` stops the PreCompact hook from even
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

MODES = ("off", "shadow", "live")

_CONFIG_NAME = "session_ledger_shadow.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "shadow",
}


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
        logger.warning("session_ledger_shadow base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("session_ledger_shadow overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the worker must run under: ``off`` or ``shadow`` — read live.

    Master ``enabled: false`` → ``off``. ``live`` is reserved until the
    data-gated flip PR implements the live write path — coerced to
    ``shadow`` with a warning so a premature flip is loud and harmless.
    An invalid value degrades to ``shadow`` (observable, never a silent
    off, never live).
    """
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # A hand-edited unquoted `mode: off` parses as YAML-1.1 boolean
        # False. That intent is unambiguous — honor it.
        return "off"
    if mode == "live":
        logger.warning(
            "session_ledger_shadow mode 'live' is reserved (flip PR pending) — coercing to shadow"
        )
        return "shadow"
    if mode not in MODES:
        logger.warning("session_ledger_shadow has invalid mode %r — degrading to shadow", mode)
        return "shadow"
    return mode
