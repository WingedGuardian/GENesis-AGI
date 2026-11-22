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
_REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")  # GitHub repo-name charset


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


# The reconciler re-checks this immediately before each of its writes.
def writes_allowed() -> bool:
    """True only in ``live`` — the single predicate a GitHub writer checks."""
    return effective_mode() == "live"


def builder_launch_armed() -> tuple[bool, str]:
    """Whether the builder launcher may start builders, and why not if it may not.

    Armed only by a hand edit of the overlay, ``builder_launch: {enabled: true}``
    with the literal boolean, on a board whose configured ``mode`` is valid as
    written (a degraded mode never arms) and is not ``off``. ``propose_only``
    arms too: a launch writes nothing to GitHub. Every other value fails closed.

    Only ``builder_launch.enabled: false`` or the kill switch DISARMS: a session
    cannot change either (``settings_update`` refuses the ``builder_launch``
    key). ``mode: off`` is not a disarm a session cannot undo, since a session
    may set the board mode back to ``propose_only``. While the mode is off each
    pass records ``launcher_disabled``, so setting it back starts a new enable
    cutoff: cards already in Build are listed to the owner and never launch.
    """
    if _kill_switch_on():
        return False, "board kill switch set"
    cfg = load_config()
    section = cfg.get("builder_launch")
    if not isinstance(section, dict) or section.get("enabled") is not True:
        return False, "builder_launch.enabled is not true"
    mode = effective_mode()
    if mode == "off":
        return False, "board mode is off (not a lasting disarm: use builder_launch.enabled: false)"
    if cfg.get("mode") != mode:
        return False, f"board mode {cfg.get('mode')!r} is invalid (degraded to {mode})"
    return True, "armed"


#: The launcher's levers (``builder_launch:`` in the overlay), each with its
#: default and the range a hand edit may set. Out of range or malformed reads
#: as the default (logged), never as a looser value.
BUILDER_LAUNCH_DEFAULTS: dict[str, tuple[int, int, int]] = {
    # Live builders without an open closing PR at once (spec E rev 5).
    "capacity": (1, 1, 4),
    # Owner ruling 2026-11-14 (ego 9627ed63): a launch waits under this free memory.
    "min_free_gb": (4, 1, 64),
    # A build runs for hours and Remote Control dies with the login.
    "login_margin_hours": (6, 1, 48),
    # Builders take the lowest free slot from here, so the owner's usual
    # low-numbered slots never open onto a builder's pane.
    "first_slot": (9, 1, 9999),
}


def builder_launch_settings() -> dict[str, int]:
    """The launcher's levers, defaulted and range-checked."""
    section = load_config().get("builder_launch")
    section = section if isinstance(section, dict) else {}
    out = {}
    for name, (default, low, high) in BUILDER_LAUNCH_DEFAULTS.items():
        value = section.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            logger.warning("builder_launch.%s=%r is invalid; using %s", name, value, default)
            value = default
        out[name] = value
    return out


def valid_login(value: object) -> bool:
    return isinstance(value, str) and bool(_LOGIN.match(value))


def valid_project_number(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def tracker_repo() -> tuple[str, str] | None:
    """``(owner, name)`` of the public tracker promotions post to and the board
    links — the configured ``github.user`` / ``github.public_repo``, never the
    checkout's own remote (on a fork clone that is the operator's fork). None
    when either half is missing or malformed: callers refuse rather than guess."""
    from genesis.env import github_public_repo, github_user

    owner, name = github_user(), github_public_repo()
    if not (valid_login(owner) and isinstance(name, str) and _REPO_NAME.match(name)):
        return None
    return owner, name


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
