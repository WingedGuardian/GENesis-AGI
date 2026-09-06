"""Federation control surface — the live-read mode lever + constants.

The ONE place the federation subsystem consults for policy (contributor_worklog
lineage). A private cross-owner channel is OPT-IN, so unlike the work-log the
DEFAULT is ``off`` and an invalid/broken config degrades to ``off`` (fully dark),
never to a mode that would receive or send — the least-authority direction for a
subsystem that talks to another person's install.

Modes:
- ``off`` — subsystem dark: no poll loop, no pairing, no send. (Default + the
  emergency stop.)
- ``propose_only`` — the safe operational mode and the v1 target: the poll loop
  runs, inbound peer messages are verified + quarantined + QUEUED for the human,
  and every outbound message is HELD for owner approval. NOTHING auto-acts.
- ``live`` — reserved for v2 (per-contact earned-trust automation). v1 has no
  trust engine wired, so ``live`` behaves exactly like ``propose_only`` (every
  action still proposed); it exists so the wire/lever are forward-compatible.

Kill switch: ``GENESIS_FEDERATION_DISABLED=1`` forces ``off`` regardless of
config — the background-safe brake (a dispatched session can never be made to
pair or send by a config edit).

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import _resolve_overlay_path, merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)

MODES = ("off", "propose_only", "live")

# approval_requests.action_type stamped on a held OUTBOUND peer message. Free
# text (approval_requests has no enum) but subsystem-specific so the dashboard
# approve-all exclusion and the send watcher match on this exact value.
FEDERATION_SEND_ACTION_TYPE = "federation_peer_send"

# Env kill switch — forces `off`.
_DISABLE_ENV = "GENESIS_FEDERATION_DISABLED"

_CONFIG_NAME = "federation.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,  # master flag; mode still gates behaviour (default off)
    "mode": "off",  # OPT-IN: a fresh clone never federates until the owner enables it
    "retention_days": 90,  # transcript bodies pruned after this (chain skeleton kept forever)
    "max_message_bytes": 16384,  # reject an inbound/outbound plaintext larger than this
    "poll_interval_seconds": 300,  # relay poll cadence when the subsystem is active
}

_INT_KNOBS = (
    "retention_days",
    "max_message_bytes",
    "poll_interval_seconds",
)


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def _kill_switch_on() -> bool:
    return os.environ.get(_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache. Deep-merges
    (DEFAULTS ← base yaml ← .local.yaml overlay); missing/corrupt files degrade
    layer-by-layer toward DEFAULTS."""
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except FileNotFoundError:
        pass  # shipped default may be absent; DEFAULTS (off) is correct
    except Exception:
        logger.warning("federation base config unreadable at %s", base_path)
    # FAIL-CLOSED on a corrupt/unreadable overlay. The overlay is where the user's
    # own `mode: off` would live, so if we can't reliably read it we must NOT fall
    # back to an active base config. NB: merge_local_overlay() SWALLOWS a YAML
    # parse error and returns base (fail-open), so we validate the overlay
    # OURSELVES first — a present-but-unparseable overlay, or one that isn't a
    # mapping, forces off. (An absent overlay, or an empty one parsing to None, is
    # a legitimate no-op.)
    overlay_path = _resolve_overlay_path(base_path)
    if overlay_path.exists():
        try:
            parsed = yaml.safe_load(overlay_path.read_text())
        except Exception:
            logger.warning("federation overlay is malformed YAML — forcing off (fail-closed)")
            return _forced_off(merged, base)
        if parsed is not None and not isinstance(parsed, dict):
            logger.warning("federation overlay is not a mapping — forcing off (fail-closed)")
            return _forced_off(merged, base)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("federation overlay merge failed — forcing off (fail-closed)", exc_info=True)
        return _forced_off(merged, base)
    merged.update(base)
    return merged


def _forced_off(merged: dict[str, Any], base: dict[str, Any]) -> dict[str, Any]:
    """Return a config that resolves to ``off`` regardless of the base file — the
    fail-closed result when the local overlay cannot be trusted."""
    merged.update(base)
    merged["enabled"] = False
    merged["mode"] = "off"
    return merged


def effective_mode() -> str:
    """The mode the subsystem must run under — read live (no cache).

    Env kill switch OR ``enabled: false`` → ``off``. An invalid/unknown value
    degrades to ``off`` (fully dark) — never a silent receive/send. A hand-edited
    unquoted ``mode: off`` parses as YAML-1.1 boolean False and is honored.
    """
    if _kill_switch_on():
        return "off"
    cfg = load_config()
    # Fail-CLOSED on the master switch: only a real boolean True keeps it on. A
    # hand-edited `enabled: "false"` (string), a 0/1, or any non-bool is truthy
    # and would otherwise leave a cross-owner channel ACTIVE against the user's
    # apparent intent — degrade every non-True value to off.
    if cfg.get("enabled", True) is not True:
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        return "off"
    if mode not in MODES:
        logger.warning("federation has invalid mode %r — degrading to off", mode)
        return "off"
    return mode


def is_active() -> bool:
    """True when the subsystem should run its loops (mode is not ``off``)."""
    return effective_mode() != "off"


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never crashes a
    consumer or zeroes a limit."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value
