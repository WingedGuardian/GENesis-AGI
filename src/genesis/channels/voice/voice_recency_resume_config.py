"""Config lever for voice cross-session recency resume.

Clone of ``voice_act_config`` (fresh-read-per-call, MODES tuple + env kill
switch, degrade-toward-LESS-authority on damage). Gates whether the S2S voice
system prompt is pre-loaded with the tail of the user's most-recent prior voice
conversation as REFERENCE-ONLY context — the model must not proactively resume
it, only use it if the user asks or says something directly connected.

Modes:
  - ``off``  — no recency block is injected (the default). A fresh prompt-shaping
    surface ships dark and is armed to ``live`` only after a live voice E2E
    (mirrors ``voice_act`` / session_ledger_shadow shipping shadow-first).
  - ``live`` — the recency block is assembled and injected at session start.

This is a READ-only surface (it injects the user's OWN prior words into the
user's OWN next prompt), but it ships behind the same two-state lever so it can
be armed deliberately after E2E and killed instantly if it misbehaves. A
missing/corrupt config degrades to DEFAULTS; an invalid ``mode`` degrades to
``off`` (LEAST authority — never silently inject on bad config). The env kill
switch ``GENESIS_VOICE_RECENCY_RESUME_DISABLED=1`` forces ``off``.

Tuning keys (read live, each fail-safe to its default):
  - ``scope``          — ``global`` (latest voice conversation on ANY device;
    the default) or ``per_device`` (latest for the calling satellite; requires
    the ``cc_sessions.satellite_id`` column to be populated).
  - ``max_turns``      — how many trailing messages to include (default 6).
  - ``max_chars``      — hard cap on the assembled block (default 800).
  - ``max_age_hours``  — only resume a conversation whose last activity is newer
    than this many hours; ``null`` = no age limit (the default).

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``MODES`` from here (never
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

MODES = ("off", "live")
SCOPES = ("global", "per_device")

_CONFIG_NAME = "voice_recency_resume.yaml"

_ENV_KILL_SWITCH = "GENESIS_VOICE_RECENCY_RESUME_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "off",
    "scope": "global",
    "max_turns": 6,
    "max_chars": 800,
    "max_age_hours": None,
}


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
        logger.warning("voice_recency_resume base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("voice_recency_resume overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def _positive_int(cfg: dict[str, Any], key: str, default: int) -> int:
    val = cfg.get(key, default)
    # bool is an int subclass — reject it explicitly so `true`/`false` never
    # slips through as 1/0.
    if isinstance(val, bool) or not isinstance(val, int) or val <= 0:
        return default
    return val


def _positive_float_or_none(cfg: dict[str, Any], key: str) -> float | None:
    val = cfg.get(key)
    if val is None:
        return None  # unset — no age limit is the documented default
    if isinstance(val, bool) or not isinstance(val, (int, float)) or val <= 0:
        # Present but invalid: warn (mirroring mode/scope) and fall back to the
        # no-limit default. There is no "safer" bounded fallback — unlimited IS
        # the default — and the write path is validator-guarded, so this only
        # trips on a hand-edited yaml.
        logger.warning("voice_recency_resume %s=%r is invalid — ignoring the age limit", key, val)
        return None
    return float(val)


def resolved() -> dict[str, Any]:
    """All effective settings in ONE fresh read, each fail-safe to its default.

    ``mode`` honors the env kill switch + ``enabled`` + invalid→off; the tuning
    keys each degrade to their DEFAULT on a bad/mistyped value. Callers building
    the recency block should use this (one file read) rather than the individual
    accessors.
    """
    cfg = load_config()
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        mode = "off"
    else:
        enabled = cfg.get("enabled", True)
        raw_mode = cfg.get("mode")
        if not isinstance(enabled, bool) or not enabled:
            # A hand-edited / overlay non-boolean `enabled` (e.g. the YAML string
            # "false", which is truthy) must never leave the surface on.
            mode = "off"
        elif raw_mode is False:
            # Hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
            mode = "off"
        elif raw_mode not in MODES:
            logger.warning("voice_recency_resume has invalid mode %r — degrading to off", raw_mode)
            mode = "off"
        else:
            mode = raw_mode
    scope = cfg.get("scope")
    if scope not in SCOPES:
        if scope is not None:
            logger.warning("voice_recency_resume has invalid scope %r — degrading to global", scope)
        scope = "global"
    return {
        "mode": mode,
        "scope": scope,
        "max_turns": _positive_int(cfg, "max_turns", 6),
        "max_chars": _positive_int(cfg, "max_chars", 800),
        "max_age_hours": _positive_float_or_none(cfg, "max_age_hours"),
    }


def effective_mode() -> str:
    """The mode recency-resume runs under — read live. Fail-closed to ``off``."""
    return resolved()["mode"]
