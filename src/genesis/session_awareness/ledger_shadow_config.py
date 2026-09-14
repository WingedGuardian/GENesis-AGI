"""Ledger shadow extractor control surface — live-read mode lever.

The ONE place the ledger shadow worker consults for policy (session-manager
PR-3, ws3_immunity lineage):

- :func:`effective_mode` — ``off | shadow | live``, re-read from the merged
  YAML (``config/session_ledger_shadow.yaml`` + the user overlay
  ``~/.genesis/config/session_ledger_shadow.local.yaml``) on EVERY call.
  No boot cache — a ``settings_update`` or hand edit takes effect on the
  next PreCompact-spawned worker instantly (each run is a fresh process
  anyway). ``live`` promotes qualifying proposals into the real ledger;
  it was reserved until the write path existed, and now it does. Because
  old releases accepted (and coerced) ``live``, honoring it additionally
  requires the renewed opt-in ``live_opt_in: true`` — a key no legacy
  overlay can carry.

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
    # Renewed opt-in for live writes. `mode: live` was accepted (and coerced)
    # by releases that predate the write path, so the value alone cannot prove
    # current intent — this key did not exist back then, so no legacy overlay
    # can carry it.
    "live_opt_in": False,
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
    """The mode the worker must run under: ``off``, ``shadow`` or ``live``.

    Read live, per call. Master ``enabled: false`` → ``off``.

    Every branch here fails toward LESS write authority — a typo or a corrupt
    config can only ever cost one Haiku call per compaction, never a live
    ledger write:

    - ``enabled`` must be the boolean ``True``. Any other value — including
      the YAML *string* ``"false"``, which is truthy in Python — disables.
    - ``mode: live`` alone grants nothing. Releases before the write path
      existed accepted and persisted ``live`` while documenting it as
      reserved, so an overlay written back then must not begin autonomous
      ledger writes on upgrade. Live additionally requires the renewed
      opt-in ``live_opt_in: true`` (see DEFAULTS).
    - An invalid mode degrades to ``shadow`` (observable, never a silent
      off, never live).
    """
    cfg = load_config()
    enabled = cfg.get("enabled", True)
    if enabled is not True:
        if enabled is not False:
            logger.warning(
                "session_ledger_shadow has non-boolean enabled %r — treating as off",
                enabled,
            )
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # A hand-edited unquoted `mode: off` parses as YAML-1.1 boolean
        # False. That intent is unambiguous — honor it.
        return "off"
    if mode == "live" and cfg.get("live_opt_in") is not True:
        logger.warning(
            "session_ledger_shadow mode 'live' without live_opt_in: true — "
            "coercing to shadow. A legacy overlay predating the write path "
            "must not go live on upgrade; set BOTH keys to enable live writes."
        )
        return "shadow"
    if mode not in MODES:
        logger.warning("session_ledger_shadow has invalid mode %r — degrading to shadow", mode)
        return "shadow"
    return mode
