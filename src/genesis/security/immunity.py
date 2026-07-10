"""WS-3 immunity control surface — live-read kill switch + gate helpers.

This module is the ONE place B1's provenance gates consult for policy:

- :func:`gate_mode` — per-gate ``off | shadow | enforce``, re-read from the
  merged YAML (``config/ws3_immunity.yaml`` + the user overlay
  ``~/.genesis/config/ws3_immunity.local.yaml``) on EVERY call, cc_roster
  style. No boot cache — a ``settings_update`` or hand edit takes effect in
  the running process instantly. Master ``enabled: false`` short-circuits
  every gate to ``off``.
- :func:`is_blockable` — THE never-block-owner invariant. Only
  ``external_untrusted`` is ever blockable; ``owner``/``first_party`` return
  False by construction in every mode. Unknown/missing values normalize to
  ``external_untrusted`` (fail-closed at GATE time — store-time derivation
  in ``genesis.memory.provenance`` never does this).
- :func:`record_demotion` — auto-demote scaffold (wired in B1): writes
  ``{gate: {mode: shadow}}`` plus an audit entry into the SAME overlay file
  ``gate_mode`` reads, so demotion state and gate behavior cannot drift
  apart. Never demotes below shadow, never escalates.

Failure posture: a missing/corrupt config degrades to DEFAULTS
(master on, every gate shadow) — shadow never blocks, so config damage can
only ever make the system MORE permissive-but-observable, never lock the
owner out.

Dependency rule: this module must stay importable from anywhere
(memory/, learning/, mcp/) — stdlib + yaml + genesis.env +
genesis._config_overlay only. It must NEVER import genesis.mcp.health.settings
(settings imports GATES/MODES from here; one-way).
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import _user_config_dir, merge_local_overlay
from genesis.env import repo_root
from genesis.memory.provenance import (
    ORIGIN_CLASSES,
    ORIGIN_EXTERNAL_UNTRUSTED,
)

logger = logging.getLogger(__name__)

GATES = ("procedure", "identity", "autonomy", "injection")
MODES = ("off", "shadow", "enforce")

_CONFIG_NAME = "ws3_immunity.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    **{gate: {"mode": "shadow"} for gate in GATES},
    "auto_demote": {
        "enabled": True,
        "window_minutes": 60,
        "would_block_threshold": 5,
    },
}


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_immunity_config() -> dict[str, Any]:
    """Read the merged immunity config fresh — per call, NO cache.

    Deep-merges (defaults ← base yaml ← .local.yaml overlay). Missing or
    corrupt files degrade layer-by-layer toward DEFAULTS (master on, all
    gates shadow — never silently ``enforce``, never silently ``off``).
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        logger.warning("ws3_immunity base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("ws3_immunity overlay merge failed", exc_info=True)
    for key, value in base.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def gate_mode(gate: str) -> str:
    """Effective mode for *gate*: ``off | shadow | enforce`` — read live.

    Unknown gate names raise ``ValueError`` (a typo at a gate call site is a
    bug, not a policy decision). Master ``enabled: false`` → ``off`` for
    every gate. An invalid mode VALUE in the file degrades to ``shadow``
    with a warning (observable, never blocking, never silently off).
    """
    if gate not in GATES:
        raise ValueError(f"unknown immunity gate {gate!r}; expected one of {GATES}")
    cfg = load_immunity_config()
    if not cfg.get("enabled", True):
        return "off"
    gate_cfg = cfg.get(gate)
    mode = gate_cfg.get("mode") if isinstance(gate_cfg, dict) else None
    if mode not in MODES:
        logger.warning(
            "ws3_immunity gate %s has invalid mode %r — degrading to shadow",
            gate, mode,
        )
        return "shadow"
    return mode


def effective_origin_class(value: str | None) -> str:
    """GATE-TIME normalizer: unknown/missing origin → ``external_untrusted``.

    This is the ONLY place the fail-closed unknown→untrusted rule lives.
    Store-time derivation (``provenance.derive_origin_class``) stays
    conservative-first-party for internal writers; a NULL/garbage value that
    reaches a gate is treated as untrusted so gates fail closed, never open.
    """
    if value in ORIGIN_CLASSES:
        return value  # type: ignore[return-value]  # membership proves str
    return ORIGIN_EXTERNAL_UNTRUSTED


def is_blockable(origin_class: str | None) -> bool:
    """THE never-block-owner/first-party invariant.

    Only ``external_untrusted`` (including anything unknown, via the
    fail-closed normalizer) is ever blockable. ``owner`` and ``first_party``
    are False by construction in EVERY mode — B1 gates must route every
    block decision through this helper.
    """
    return effective_origin_class(origin_class) == ORIGIN_EXTERNAL_UNTRUSTED


def _overlay_path() -> Path:
    return _user_config_dir() / "ws3_immunity.local.yaml"


def record_demotion(gate: str, reason: str) -> None:
    """Auto-demote scaffold: set *gate* to shadow + write an audit entry.

    Writes into the ``.local.yaml`` overlay — the exact file
    :func:`gate_mode` reads — so the demotion is effective on the very next
    gate call, survives restarts, and is human-visible/editable. Never sets
    ``off``, never escalates. Atomic tempfile+rename write; the residual
    read-merge-write race with a concurrent ``settings_update`` is
    last-writer-wins on DISJOINT keys (documented trade; B0 has no callers —
    B1 wires the would-block counters that invoke this).
    """
    if gate not in GATES:
        raise ValueError(f"unknown immunity gate {gate!r}; expected one of {GATES}")
    path = _overlay_path()
    overlay: dict[str, Any] = {}
    try:
        if path.is_file():
            loaded = yaml.safe_load(path.read_text()) or {}
            if isinstance(loaded, dict):
                overlay = loaded
    except Exception:
        logger.warning("ws3_immunity overlay unreadable; rewriting", exc_info=True)

    previous = gate_mode(gate)
    overlay.setdefault(gate, {})
    overlay[gate]["mode"] = "shadow"
    state = overlay.setdefault("auto_demote_state", {})
    state[gate] = {
        "demoted_at": datetime.now(UTC).isoformat(),
        "from_mode": previous,
        "reason": reason,
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=".ws3_immunity.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as fh:
            yaml.safe_dump(overlay, fh, default_flow_style=False, sort_keys=False)
        os.replace(tmp_name, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    logger.warning(
        "ws3_immunity AUTO-DEMOTE: gate %s %s -> shadow (%s) — state: %s",
        gate, previous, reason, json.dumps(state[gate]),
    )
