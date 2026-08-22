"""Cheap, fail-safe reads of the autonomy *config* — the shipped default posture only.

The authoritative, per-install autonomy state lives in the ``autonomy_states`` DB
table and is served by :class:`genesis.autonomy.state_machine.AutonomyStateMachine`,
whose ``_load_config`` is coupled to an ``aiosqlite`` connection. Surfaces that only
need "what default level does this install ship / start at" — e.g. the dashboard
``setup-status`` readiness enrichment — cannot afford a DB hit or an async context on
the request path, so this module offers a pure, synchronous, never-raising read of
``config/autonomy.yaml``'s ``defaults`` block.

This is the CONFIG default, NOT the live earned level. Do not use it where the actual
current per-install level matters (that requires the state machine + DB).
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from genesis.autonomy.types import AutonomyLevel
from genesis.env import repo_root

logger = logging.getLogger(__name__)

# The shipped autonomy config. Module-level so tests can point the reader at a temp
# file via the ``path`` arg without touching the YAML parsing itself.
_AUTONOMY_CONFIG_PATH = repo_root() / "config" / "autonomy.yaml"

# Conservative fallback for ANY read failure: L1 is the shipped default for every
# category ("conservative start"), so a bad/missing config degrades to that floor
# rather than inventing a higher, more-permissive level.
_DEFAULT_LEVEL = 1


def read_autonomy_default_level(
    category: str = "direct_session", *, path: Path | None = None
) -> int:
    """The shipped *default* autonomy level for ``category`` (fail-safe → 1).

    Reads ``config/autonomy.yaml``'s ``defaults`` block with ``yaml.safe_load`` and
    returns the level validated against the constructible :class:`AutonomyLevel` enum.
    Returns ``1`` on ANY failure — missing file, unreadable/non-UTF-8 file, malformed
    YAML, absent ``defaults``/``category`` key, a non-int value, or a value outside the
    constructible level range (0/negative, or 5–7 which are not yet enum members) —
    mirroring ``AutonomyStateMachine._load_config``'s graceful fallback, so a surface
    that reads this (the readiness enrichment) can never be 500'd or advertise a level
    the runtime couldn't actually construct.

    ``category`` defaults to ``direct_session`` — the autonomy category governing
    autonomous CC sessions, the posture most relevant to the "Autonomous" readiness
    tier. This is the CONFIG default, not the live earned level (per-install DB state).
    """
    cfg_path = path if path is not None else _AUTONOMY_CONFIG_PATH
    try:
        data = yaml.safe_load(cfg_path.read_text())
        # A top-level scalar/list (e.g. a truncated or hand-mangled file) parses to a
        # non-dict; guard so `.get` can't AttributeError and break the never-raise
        # contract. Same for a non-mapping `defaults:` value.
        defaults = data.get("defaults") if isinstance(data, dict) else None
        if not isinstance(defaults, dict):
            return _DEFAULT_LEVEL
        # Validate against the CONSTRUCTIBLE AutonomyLevel enum (currently L1–L4; the
        # yaml `ceilings` cap ACTIONS, not the level enum — L5–L7 are deferred to V5 and
        # AutonomyStateMachine.load_or_create_defaults does `AutonomyLevel(level)`, which
        # ValueErrors on an out-of-range default). `AutonomyLevel(int(...))` raises for
        # any non-constructible value (0, negatives, 5–7), which the except below maps to
        # the L1 fail-safe — so this display read can only ever advertise a real level,
        # and auto-tracks the enum if V5 widens it.
        return int(AutonomyLevel(int(defaults[category])))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        yaml.YAMLError,
    ):
        # OverflowError: `int(float('inf'))` from a YAML `.inf` level. KeyError: absent
        # category. AttributeError: non-dict slipping past the guard. All → conservative L1.
        logger.debug(
            "autonomy default-level read failed for %s (%s); using L%d",
            category,
            cfg_path,
            _DEFAULT_LEVEL,
            exc_info=True,
        )
        return _DEFAULT_LEVEL
