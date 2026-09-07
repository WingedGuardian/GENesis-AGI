"""Desktop-takeover control surface — the arming lever for real input.

The ONE place :mod:`genesis.autonomy.desktop_gate` consults for policy
(``ledger_shadow_config`` / ``contributor_worklog_config`` lineage):

- :func:`effective_mode` — ``off | shadow | live``, re-read from the merged
  YAML (``config/desktop_takeover.yaml`` + the user overlay
  ``~/.genesis/config/desktop_takeover.local.yaml``) on EVERY call. No boot
  cache: the gate re-reads per action, so a hand edit takes effect on the next
  action with no restart.
- :func:`grant_ttl` / :func:`action_ttl` — the two bounded windows, coerced to
  their safe defaults on any unusable value.

Why the arming path is deliberately awkward: a live desktop grant is the
broadest authority Genesis holds — everything the operator can do at their own
machine. ``mode: live`` alone grants nothing; ``live_opt_in: true`` must be set
as well, so a copied config, a legacy overlay or a single careless edit cannot
hand over the keyboard. Neither key is exposed through ``settings_update`` or
the dashboard (this module is deliberately absent from
``mcp/health/settings.py``): arming is a conscious edit of the gitignored
overlay, never one unconfirmed API call.

Every degradation path here moves toward LESS authority. An invalid mode is
``shadow`` (observable) rather than ``off`` (silently inert) or ``live``.

Kill switch: ``GENESIS_DESKTOP_TAKEOVER_DISABLED=1`` forces ``off`` before any
YAML is read, so a config the process cannot parse is not a way past the stop.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only.
The gate imports from here, never the reverse (one-way).
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

MODES = ("off", "shadow", "live")

#: Forces ``off`` from every consumer, ahead of the config read.
DISABLE_ENV = "GENESIS_DESKTOP_TAKEOVER_DISABLED"

_CONFIG_NAME = "desktop_takeover.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "shadow",
    # Renewed opt-in for real input — `mode: live` alone is not consent.
    "live_opt_in": False,
    # Bounds on the owner's session grant and on a single action in transit.
    # See config/desktop_takeover.yaml for the reasoning behind each value.
    "grant_ttl_minutes": 30,
    "action_ttl_seconds": 30,
}


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Deep-merges (defaults <- base yaml <- .local.yaml overlay). A missing or
    corrupt file degrades layer-by-layer toward DEFAULTS, which are ``shadow``
    and un-opted-in: config damage can never arm the capability.
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        logger.warning("desktop_takeover base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("desktop_takeover overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the gate must honour: ``off``, ``shadow`` or ``live``.

    Read live, per call. Order matters, and every branch fails toward less
    authority:

    - the env kill switch outranks the file, and is checked BEFORE any YAML is
      read so an unparseable config is not a way around the stop;
    - ``enabled`` must be the boolean ``True``. Any other value — including the
      YAML *string* ``"false"``, which is truthy in Python — reads as ``off``;
    - YAML 1.1 parses a bare ``mode: off`` as boolean ``False``; that intent is
      unambiguous, so it is honoured rather than rejected;
    - ``mode: live`` without ``live_opt_in: true`` coerces to ``shadow``. Two
      keys, both affirmative, are what arming the keyboard costs;
    - any other invalid mode degrades to ``shadow`` — observable, never a
      silent ``off``, never ``live``.
    """
    if os.environ.get(DISABLE_ENV) == "1":
        return "off"
    cfg = load_config()
    enabled = cfg.get("enabled", True)
    if enabled is not True:
        if enabled is not False:
            logger.warning(
                "desktop_takeover has non-boolean enabled=%r — treating as off", enabled
            )
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        return "off"
    if mode == "live" and cfg.get("live_opt_in") is not True:
        logger.warning(
            "desktop_takeover mode 'live' without live_opt_in: true — coercing to "
            "shadow. Arming real input requires BOTH keys; one of them arriving by "
            "accident must not hand over the keyboard."
        )
        return "shadow"
    if mode not in MODES:
        logger.warning("desktop_takeover has invalid mode %r — degrading to shadow", mode)
        return "shadow"
    return mode


def _positive_int(cfg: dict[str, Any], key: str) -> int:
    """A positive int from config, or the DEFAULT — never 0 and never negative.

    A zero or negative window would expire every grant/action instantly, which
    reads as "the capability is broken" rather than "the capability is off";
    the stop switch is ``mode: off``, not a mistyped duration. A value that
    cannot be used falls back to the default rather than to no bound at all.
    """
    raw = cfg.get(key, DEFAULTS[key])
    if isinstance(raw, bool):  # bool is an int subclass; `true` is not a duration
        raw = None
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("desktop_takeover %s=%r is not an integer — using default", key, raw)
        return int(DEFAULTS[key])
    if value <= 0:
        logger.warning("desktop_takeover %s=%r is not positive — using default", key, raw)
        return int(DEFAULTS[key])
    return value


def grant_ttl_minutes() -> int:
    """How long an owner's session grant stays valid after they approved it."""
    return _positive_int(load_config(), "grant_ttl_minutes")


def action_ttl_seconds() -> int:
    """Freshness window stamped on one allowed action (enforced device-side)."""
    return _positive_int(load_config(), "action_ttl_seconds")
