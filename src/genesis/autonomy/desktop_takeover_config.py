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


#: Set on the merged config when the OVERLAY existed but could not be read.
#:
#: Scoped to the overlay ON PURPOSE, and the asymmetry is the whole argument.
#: The tracked base holds only values ``DEFAULTS`` already reproduces
#: (``enabled: true``, ``mode: shadow``, ``live_opt_in: false``, the two TTLs),
#: so falling back to DEFAULTS when it is damaged loses NOTHING operator-
#: specific — it lands on exactly what the file said. The overlay is gitignored
#: and is the sanctioned home for every operator customization, including the
#: disable: since the base ships ``enabled: true``, an ``enabled: false`` can
#: only live in the overlay. Damage there discards the off switch and reports
#: a clean load.
#:
#: This is why the module's "an invalid value degrades to shadow, never a
#: silent off" rule is NOT contradicted here. That rule is about a value we can
#: SEE and cannot interpret; this is about a file whose contents we never saw
#: at all, so there is no operator intent left to degrade politely toward.
_OVERLAY_UNREADABLE = "_overlay_unreadable"


#: Spellings that ARM the kill switch, and the ones that explicitly do not.
#: Anything else disables and says so — this is the one control the module
#: documents as unreachable-around, and an operator who typed something to stop
#: the capability meant to stop it. Honouring only the literal "1" meant
#: ``=true`` / ``=yes`` / ``=on`` disabled nothing and warned about nothing.
_KILL_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})
_KILL_FALSY = frozenset({"", "0", "false", "no", "off", "n", "f"})


def _kill_switch_set() -> bool:
    """Whether ``GENESIS_DESKTOP_TAKEOVER_DISABLED`` says stop."""
    raw = os.environ.get(DISABLE_ENV)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _KILL_TRUTHY:
        return True
    if value in _KILL_FALSY:
        return False
    logger.warning(
        "%s=%r is not a recognised boolean — treating it as SET. Someone typed "
        "a value into the kill switch; the safe reading is that they meant to "
        "stop the capability.",
        DISABLE_ENV,
        raw,
    )
    return True


def _overlay_is_damaged(path: Path) -> bool:
    """True when the overlay exists but does not parse as a YAML mapping.

    Absent is not damaged — that is the common case, and DEFAULTS are correct
    for it. An empty file (or an explicit ``null``) is not damaged either: it
    is a legitimately empty layer, which is how ``merge_local_overlay`` reads
    it too.
    """
    # `exists()`, not `is_file()`, to match `merge_local_overlay`'s own test.
    # It takes its `exists()` branch for a DIRECTORY, `read_text()` raises
    # IsADirectoryError, and it warns and returns base — the overlay silently
    # dropped. An `is_file()` check here would answer "not damaged" for that
    # same path, so `effective_mode()` would report shadow on a load where
    # every operator override was discarded: exactly the hole this function
    # exists to close, reached by a path it did not test. The try/except below
    # already handles the directory read correctly.
    if not path.exists():
        return False
    try:
        loaded = yaml.safe_load(path.read_text())
    except Exception:
        logger.warning("desktop_takeover overlay unreadable at %s", path, exc_info=True)
        return True
    if loaded is not None and not isinstance(loaded, dict):
        logger.warning(
            "desktop_takeover overlay %s has a %s at its root, not a mapping",
            path,
            type(loaded).__name__,
        )
        return True
    return False


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Deep-merges (defaults <- base yaml <- .local.yaml overlay). A missing or
    corrupt BASE degrades layer-by-layer toward DEFAULTS, which are ``shadow``
    and un-opted-in: config damage can never arm the capability.

    A corrupt OVERLAY is flagged instead of degraded, because
    :func:`merge_local_overlay` returns *base* unchanged when the overlay will
    not parse. It warns, so this is not silent — but the value it RETURNS is
    indistinguishable from a clean load, and every override in that file is
    gone. For most subsystems that fallback is right; for the arming lever of
    the desktop capability it means losing the operator's off switch and
    reporting success.
    """
    # LAZY, not module-level. `_resolve_overlay_path` is private and imported
    # rather than re-derived on purpose — the overlay location is user-dir-first
    # with a repo-relative fallback, and a local copy of that rule would drift,
    # which here means probing a file nobody wrote and reporting "clean" for a
    # damaged overlay. But a module-level alias binding of it trips the
    # user-config-binding guard (tests/test_config_overlay.py): such a binding
    # can outlive a test's patch of the canonical name. Importing inside the
    # function keeps one source of truth AND leaves the name resolvable at call
    # time, which is what the guard's own comment sanctions.
    from genesis._config_overlay import _resolve_overlay_path

    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        logger.warning("desktop_takeover base config unreadable at %s", base_path)

    overlay_damaged = _overlay_is_damaged(_resolve_overlay_path(base_path))
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("desktop_takeover overlay merge failed", exc_info=True)
        overlay_damaged = True

    merged.update(base)
    if overlay_damaged:
        merged[_OVERLAY_UNREADABLE] = True
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
    if _kill_switch_set():
        return "off"
    cfg = load_config()
    if cfg.get(_OVERLAY_UNREADABLE):
        # OFF, not shadow. A damaged OVERLAY means the operator's own settings
        # were discarded, and the one they are most likely to have written is
        # `enabled: false` — the tracked base ships `enabled: true`, so a
        # disable can only live there. Degrading to shadow would quietly resume
        # recording for someone who had switched it off.
        #
        # Not the "silent off" the module docstring warns against: this warns
        # on every call and names the file. And it costs no capability, since
        # neither `off` nor `shadow` can act — the choice between them is
        # purely about honouring the last intent that was legible. Fix the YAML
        # and the mode returns on its own; the config is re-read per action.
        logger.warning(
            "desktop_takeover overlay is present but unreadable — forcing off. "
            "Settings in that file are NOT in effect; fix the YAML."
        )
        return "off"
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


#: Upper bounds on the two windows. A lever the module calls "bounded" was
#: bounded on one side only: `_positive_int` rejected <= 0 and accepted
#: anything above it, so a typo'd `grant_ttl_minutes: 30000` was a 20-day grant
#: and a large enough value made `timedelta(minutes=...)` raise OverflowError
#: out of the gate's `check()` rather than refuse.
#:
#: Derived from the protocol rather than from observed values, per the rule for
#: safety bounds: a grant is session consent for minutes of work at a keyboard,
#: so a day is already far past "a grant nobody remembers giving must lapse on
#: its own"; and an action TTL bounds container -> SSH -> device transit, not
#: operator think-time, so five minutes is generous for a hop measured in
#: hundreds of milliseconds.
_MAXIMA: dict[str, int] = {
    "grant_ttl_minutes": 1440,
    "action_ttl_seconds": 300,
}


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
    ceiling = _MAXIMA.get(key)
    if ceiling is not None and value > ceiling:
        # The DEFAULT, not the ceiling. Every degradation path in this module
        # moves toward LESS authority, and clamping to the maximum would make a
        # mistyped duration grant the longest window the code allows.
        logger.warning(
            "desktop_takeover %s=%r exceeds the maximum of %d — using default %d",
            key,
            raw,
            ceiling,
            DEFAULTS[key],
        )
        return int(DEFAULTS[key])
    return value


def grant_ttl_minutes() -> int:
    """How long an owner's session grant stays valid after they approved it."""
    return _positive_int(load_config(), "grant_ttl_minutes")


def action_ttl_seconds() -> int:
    """Freshness window stamped on one allowed action (enforced device-side)."""
    return _positive_int(load_config(), "action_ttl_seconds")
