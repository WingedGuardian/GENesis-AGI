"""Work-board control surface — the live-read mode lever.

The ONE place every board consumer consults for its write authority (the
``contributor_worklog_config`` lineage). Re-read from the merged YAML
(``config/board.yaml`` + the user overlay ``~/.genesis/config/board.local.yaml``)
on EVERY call — no boot cache, so a ``settings_update("board", ...)`` or a hand
edit takes effect at the next reconcile tick or promotion.

Modes:

* ``off`` (shipped default) — the board does nothing: no reconcile, no
  promotion. A fresh clone has no board until its owner sets one up.
* ``propose_only`` — the board is READ: the reconciler reads the project and
  computes state, promotion holds proposals for approval, but NOTHING is
  written to GitHub (no items added, no fields set, no comments, no issues).
* ``live`` — the reconciler and an approved promotion may write to GitHub.

An INVALID mode degrades to ``propose_only`` — toward LESS write authority,
never a silent ``live`` (a public-repo write must never happen by config
accident), and never a silent ``off`` either (a board that quietly stops
reading reports a stale, confident picture).

Kill switch: ``GENESIS_BOARD_DISABLED=1`` forces ``off`` from every consumer,
folded in here so no consumer can forget it.

The open-question tools are deliberately NOT gated by this lever: they write
only local rows, never GitHub, and refusing to record a question would lose it.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)

MODES = ("off", "propose_only", "live")

_DISABLE_ENV = "GENESIS_BOARD_DISABLED"
_CONFIG_NAME = "board.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "off",
    # Which Projects v2 board: written by scripts/board_setup.py into the user
    # overlay. Install-specific, so the shipped config never carries a value.
    "project_owner": None,
    "project_number": None,
}

_LOGIN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def _kill_switch_on() -> bool:
    return os.environ.get(_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Deep-merges (defaults <- base yaml <- .local.yaml overlay). Missing or
    corrupt files degrade layer by layer toward DEFAULTS.
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        logger.warning("board base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("board overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode every board consumer must run under — read live.

    Env kill switch, OR a master ``enabled`` that is anything but the literal
    boolean ``True`` (``false``, ``0``, a bare ``enabled:``, a quoted string)
    -> ``off``: the master switch fails CLOSED, so a hand edit that blanks it
    can never leave a ``live`` mode armed. An invalid ``mode`` degrades to
    ``propose_only`` (reads still work, nothing is written).
    """
    if _kill_switch_on():
        return "off"
    cfg = load_config()
    if cfg.get("enabled", True) is not True:
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # A hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
        return "off"
    if mode not in MODES:
        logger.warning("board has invalid mode %r — degrading to propose_only", mode)
        return "propose_only"
    return mode


def writes_allowed() -> bool:
    """True only in ``live`` — the single predicate a GitHub writer checks."""
    return effective_mode() == "live"


def valid_login(value: object) -> bool:
    return isinstance(value, str) and bool(_LOGIN.match(value))


def valid_project_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


# GROUNDWORK(board-reconciler): read by the board reconciler (the next board PR)
# to find the project scripts/board_setup.py recorded.
def project_ref() -> tuple[str, int] | None:
    """``(owner_login, project_number)`` of the configured board, or None when
    setup has not run (or the overlay holds a malformed value, logged)."""
    cfg = load_config()
    owner, number = cfg.get("project_owner"), cfg.get("project_number")
    if owner is None and number is None:
        return None
    if not (valid_login(owner) and valid_project_number(number)):
        logger.warning(
            "board project_owner/project_number malformed (%r, %r) — treated as unset",
            owner,
            number,
        )
        return None
    return owner, number
