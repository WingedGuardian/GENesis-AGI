"""Config lever and zone choice for clock alignment (``scripts/tz_align.sh``).

``mode: live`` (default) lets the align job set the host and container clocks
to the install's zone; ``off`` stops it, on the timer and the dashboard path
alike. An invalid mode, or a config file that is there but cannot be read,
reads as ``off``: moving a system clock is the one thing this lever
authorises, so damage never authorises it. The env kill
switch ``GENESIS_TZ_ALIGN_DISABLED=1`` forces ``off``.

Cloned from ``memory.entity_adjudication_config``: fresh read per call,
defaults <- ``config/tz_align.yaml`` <- its ``.local.yaml`` overlay.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import _local_config, repo_root

logger = logging.getLogger(__name__)

MODES = ("off", "live")
DEFAULTS: dict[str, Any] = {"enabled": True, "mode": "live"}
_ENV_KILL_SWITCH = "GENESIS_TZ_ALIGN_DISABLED"
# The shipped genesis.yaml carries `timezone: "UTC"` as a placeholder, so UTC
# counts as chosen only when the dashboard route that writes the zone also wrote
# `timezone_chosen: true`. (scripts/lib/timezone.sh, used by the installers,
# treats UTC as unchosen unconditionally; it does not read that key.)
_PLACEHOLDER_ZONES = ("UTC", "Etc/UTC")


def _base_path() -> Path:
    return repo_root() / "config" / "tz_align.yaml"


def _read() -> dict[str, Any] | None:
    """The merged config, or None when a file that is there cannot be read as
    a mapping. Only an ABSENT file means "use the defaults": a damaged one may
    be an owner's ``mode: off`` that failed to parse, so it must not read as
    the default ``live``."""
    merged = copy.deepcopy(DEFAULTS)
    path = _base_path()
    base: dict[str, Any] = {}
    try:
        if path.exists():
            loaded = yaml.safe_load(path.read_text())
            if loaded is not None and not isinstance(loaded, dict):
                raise ValueError(f"{path} is not a mapping")
            base = loaded or {}
        base = merge_local_overlay(base, path, strict=True)
    except Exception:
        logger.warning("tz_align config unreadable — reading the lever as off", exc_info=True)
        return None
    merged.update(base)
    return merged


def load_config() -> dict[str, Any]:
    """The merged config, read fresh per call (DEFAULTS with ``mode: off`` when
    a file is damaged, matching ``effective_mode``)."""
    cfg = _read()
    return cfg if cfg is not None else {**DEFAULTS, "mode": "off"}


def effective_mode() -> str:
    """``live`` or ``off``, read live (kill switch, ``enabled``, then ``mode``)."""
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return "off"
    cfg = _read()
    # Only a real `true` enables: a quoted "false" is truthy, and damage reads as off.
    if cfg is None or cfg.get("enabled", True) is not True:
        return "off"
    mode = cfg.get("mode")
    if mode not in MODES:
        if mode is not False:  # unquoted `mode: off` parses as YAML-1.1 False
            logger.warning("tz_align has invalid mode %r — reading it as off", mode)
        return "off"
    return mode


def chosen_zone() -> str:
    """The zone genesis.yaml names, or "" when none was chosen.

    Empty, non-string, unloadable, and the UTC placeholder without
    ``timezone_chosen: true`` all read as "not chosen", so an install that
    never picked a zone is never moved to UTC.
    """
    cfg = _local_config()
    zone = cfg.get("timezone")
    if not isinstance(zone, str) or not zone.strip():
        return ""
    zone = zone.strip()
    if zone in _PLACEHOLDER_ZONES and cfg.get("timezone_chosen") is not True:
        return ""
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        ZoneInfo(zone)
    except Exception:
        return ""
    return zone
