"""Contributor Work-Log control surface — live-read mode lever + constants.

The ONE place the Work-Log poster consults for policy (repo_pulse_config
lineage). The Work-Log turns curated backlog/codebase items into public
GitHub issues, gated behind per-item owner approval:

- :func:`effective_mode` — ``off | propose_only | live``, re-read from the
  merged YAML (``config/contributor_worklog.yaml`` + the user overlay
  ``~/.genesis/config/contributor_worklog.local.yaml``) on EVERY call. No
  boot cache — the drain re-reads each tick, so a ``settings_update`` or a
  hand edit takes effect immediately.

Unlike repo_pulse, ``propose_only`` is the DEFAULT and the shipped posture:
the tool proposes issues + holds each for owner approval, but the drain
DRY-RUNS the post even after approval (shadow-observes the egress once and
marks the hold ``dry_run`` — terminal). ``live`` is the escalation that
actually calls ``gh issue create``. An INVALID mode degrades to
``propose_only`` — toward LESS write authority, never a silent ``live``
(a public-repo write must never happen by config accident).

Kill switch: ``GENESIS_CONTRIBUTOR_WORKLOG_DISABLED=1`` forces ``off`` from
BOTH consumers (the ``contributor_issue_propose`` MCP tool and the poster
drain) — folded into :func:`effective_mode` here rather than a separate
hook, because both consumers read config directly (repo_pulse's hook could
not, hence its split lever).

Autonomous posting (opt-in): :func:`require_approval` (default ``True``, fail-
CLOSED) governs whether a proposed issue needs an explicit human approval before
the drain may post it. A fresh clone keeps the human gate; only a deliberate
``require_approval: false`` overlay lets the curator post autonomously (still
behind ``scan_prose`` + the ``mode`` lever). ``max_posts_per_day`` (knob_int) caps
the POST rate for a cautious rollout — excess approved holds wait for the next
window. The emergency STOP is ``mode: off`` / the env kill (freezes the drain,
including already-approved held rows); ``require_approval`` is a posture flag, not
the brake.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only.
``approval_gate`` / the MCP tool / the drain import the constants + mode from
here, never the reverse (one-way).
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

MODES = ("off", "propose_only", "live")

# The approval_requests.action_type stamped on a held issue draft. Free-text
# (approval_requests has no enum) but subsystem-agnostic — the dashboard
# approve-all exclusion and the drain both match on this exact value.
CONTRIBUTOR_ISSUE_ACTION_TYPE = "contributor_issue_post"

# Capability cell (shadow-gate) for the external GitHub-issue egress door.
# The risk class MUST be a valid ``genesis.autonomy.types.RiskClass`` member —
# it keys the shared capability-grants machinery (severity ordering, prior
# weights, the CHECK constraint), so an off-enum value would hard-fail the
# future enforce stage. The Work-Log posts a sustained stream of autonomous,
# publicly-visible issues under the owner's account → BULK, the highest
# non-financial caution tier, so standing auto-post autonomy must be earned
# against strong evidence. (Guarded by test_cell_constants_are_valid.)
CELL_DOMAIN = "github"
CELL_VERB = "issue_create"
CELL_RISK_CLASS = "bulk"

# Env kill switch — forces `off` from both consumers.
_DISABLE_ENV = "GENESIS_CONTRIBUTOR_WORKLOG_DISABLED"

_CONFIG_NAME = "contributor_worklog.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "propose_only",  # ship posture: propose + approve, but NEVER auto-post
    "retention_days": 30,  # terminal rows (posted/rejected/expired/dry_run) pruned after this
    "max_held": 25,  # backpressure: the tool refuses new proposals past this many holds
    "require_approval": True,  # SHIP-SAFE: a fresh clone keeps the human per-item gate; only
    # THIS install's overlay flips it False to let the curator post autonomously (see
    # require_approval() — fail-closed: only an explicit boolean False disables the gate).
    "max_posts_per_day": 2,  # cautious-rollout cap: max autonomous posts per rolling 24h window
    # (knob_int-coerced, so a mistyped/0/negative value can never UNCAP the poster).
}

_INT_KNOBS = (
    "retention_days",
    "max_held",
    "max_posts_per_day",
)


def normalize_title(title: str) -> str:
    """Case- and whitespace-insensitive issue-title key for dedup. Shared by the
    propose tool (DB-side dedup) and the poster drain (GitHub open-issue dedup) so
    the two dedup scopes can never drift apart."""
    return " ".join((title or "").lower().split())


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def _kill_switch_on() -> bool:
    return os.environ.get(_DISABLE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Deep-merges (defaults ← base yaml ← .local.yaml overlay). Missing or
    corrupt files degrade layer-by-layer toward DEFAULTS.
    """
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        logger.warning("contributor_worklog base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("contributor_worklog overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode both consumers must run under — read live.

    Env kill switch OR master ``enabled: false`` → ``off``. An invalid value
    degrades to ``propose_only`` (observable, no auto-post — never a silent
    ``live`` to a public repo).
    """
    if _kill_switch_on():
        return "off"
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # A hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
        return "off"
    if mode not in MODES:
        logger.warning("contributor_worklog has invalid mode %r — degrading to propose_only", mode)
        return "propose_only"
    return mode


def require_approval() -> bool:
    """Whether a proposed issue draft needs an explicit human approval before the
    poster drain may post it — read live (no cache), like :func:`effective_mode`.

    FAIL-CLOSED (the security-critical direction): only an EXPLICIT boolean
    ``False`` disables the human gate. A missing key, ``None``, a typo, or any
    non-``False`` value keeps the gate ON — a public-repo post must never go
    un-gated by a config accident. So the DEFAULT (``True``) means a fresh clone
    always requires human approval; only a deliberate ``require_approval: false``
    in this install's overlay enables autonomous (Genesis-vetted) posting.

    NOTE: this is a POSTURE flag, not the emergency stop. Flipping it back to
    ``True`` re-gates only NEW proposals — a row already auto-approved and still
    ``held`` will still post. The true stop is ``mode: off`` / the env kill
    switch, which freezes the drain (and thus every held row). See
    :func:`effective_mode`.
    """
    return load_config().get("require_approval", True) is not False


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never crashes a
    consumer or zeroes a limit."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value
