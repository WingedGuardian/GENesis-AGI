"""Zero-drop detector control surface — live-read mode lever + knobs.

The ONE place the detector worker consults for policy (repo_pulse_config
lineage): ``effective_mode`` is re-read from the merged YAML
(``config/zero_drop.yaml`` + the user overlay
``~/.genesis/config/zero_drop.local.yaml``) on EVERY call, with no boot cache —
each spawned worker is a fresh process anyway.

Modes:

- ``off`` — no sweep at all. Indistinguishable from the feature not existing.
- ``observe`` — sweep and record findings, but emit no alert observation. The
  board still fills; nothing pages.
- ``alert`` — findings additionally maintain ONE superseding observation,
  resolved automatically when the board comes clean.

``observe`` is the SHIPPED default, deliberately one notch below the design's
intent. ``infrastructure_alert`` observations carry a 3-day TTL while the
dedupe gate keys on ``resolved = 0``, so a board that is stable for weeks —
which a board full of deliberate backup branches is — re-mints the same alert
every three days. The awareness loop hit this exact class and answered it with
episode-scoped dedup; until the same is built here, filling the board is worth
more than a self-repeating alarm. The lever is one settings call away.

Blindness is reported in EVERY running mode, ``observe`` included: the lever
governs egress about FINDINGS, and a broken instrument is not a finding.

An INVALID mode degrades to ``observe`` — toward LESS egress, never toward a
silent ``off``. A silently-off detector is the exact failure this subsystem
exists to prevent: it would answer "what fell through the cracks?" with a
confident, empty, wrong zero.

Failure posture: a missing/corrupt config degrades to DEFAULTS. The hook-level
kill switch is separate and stdlib-cheap (``GENESIS_ZERO_DROP_DISABLED=1``),
because the spawning hook cannot read YAML within its budget.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports from here, never the reverse.
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

MODES = ("off", "observe", "alert")

_CONFIG_NAME = "zero_drop.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "observe",
    # One sweep an hour is plenty: the conditions are hours-old by definition
    # (the age gates below), and every session boundary would otherwise spawn one.
    "min_interval_minutes": 60,
    # Age gates — work in flight right now is not stranded work.
    "branch_min_age_hours": 12,
    "worktree_min_age_hours": 6,
    # Consecutive COMPLETED sweeps before a finding is marked escalated.
    # Escalation is visibility only; nothing auto-acts on it.
    "escalation_k": 3,
    # gh --limit for the full PR history join. MEASURED 2026-09-05: 1651 PRs
    # returned in one 6s call, so this covers the whole repo with headroom;
    # n == limit records a loud 'limit_hit' and FREEZES the branch classes
    # rather than guessing at the tail.
    "max_prs": 2000,
    # Local ancestry probes per sweep, testing a branch tip against the head
    # SHA its merged/closed PR recorded. Each is one local `merge-base` on two
    # resolved commits. MEASURED 2026-09-06: 4 needed on this install (217
    # refs / 1665 PRs), so 40 is ~10x headroom. Exceeding the cap can only ADD
    # findings — an untested pair reads as unconfirmable and FLAGS — so a
    # repository that needs more says so on the board rather than silently
    # under-reporting.
    "max_ancestry_probes": 40,
    "max_listed": 10,  # findings named inline in the alert observation
    "alert_priority": "medium",
}

_INT_KNOBS = (
    "min_interval_minutes",
    "branch_min_age_hours",
    "worktree_min_age_hours",
    "escalation_k",
    "max_prs",
    "max_ancestry_probes",
    "max_listed",
)

_PRIORITIES = ("low", "medium", "high", "critical")


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache."""
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        logger.warning("zero_drop base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("zero_drop overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the worker must run under — read live."""
    cfg = load_config()
    enabled = cfg.get("enabled", True)
    # ONLY an explicit boolean False disables. A bare `enabled:` with no value
    # parses as None, and `not None` would have silently returned "off" — the
    # exact silent-off this module's docstring says must never happen, reachable
    # by a one-character typo. Anything that is not literally False (None, a
    # stray string, a number) is not a decision to disable; it degrades to
    # ENABLED and lets the mode lever below do the validating.
    if enabled is False:
        return "off"
    if not isinstance(enabled, bool):
        logger.warning("zero_drop `enabled` is %r, not a boolean — treating as enabled", enabled)
    mode = cfg.get("mode")
    if mode is False:
        # A hand-edited unquoted `mode: off` parses as YAML-1.1 False.
        return "off"
    if mode not in MODES:
        logger.warning("zero_drop has invalid mode %r — degrading to observe", mode)
        return "observe"
    return mode


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never crashes
    the worker or zeroes a limit."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value


def alert_priority(cfg: dict[str, Any]) -> str:
    value = cfg.get("alert_priority")
    return value if value in _PRIORITIES else str(DEFAULTS["alert_priority"])
