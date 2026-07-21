"""WS-2 cognitive-ledger control surface — live-read consumer levers.

The ONE place ledger consumers consult for policy (repo_pulse_config /
ledger_shadow_config lineage). Levers:

- :func:`autonomy_feed_mode` — ``off | shadow | live`` for the P2b grader→
  autonomy earn-back feed, re-read from the merged YAML
  (``config/ws2_ledger.yaml`` + the user overlay
  ``~/.genesis/config/ws2_ledger.local.yaml``) on EVERY grading pass. No boot
  cache — the operator flips shadow→live without a restart.
- :func:`arbitration_mode` — ``off | shadow | enforce`` for the P4 ego-proposal
  arbitration discount (design §5.1). ``shadow`` annotates proposals with
  calibration badges/escalation notes in the digest but never changes sort
  order; ``enforce`` additionally lets the calibrated confidence drive the
  digest sort. The discount only ever annotates — it NEVER suppresses a
  proposal from the digest (owner-facing delivery is not gated; sovereignty
  invariant, design §5).

Shadow is the DEFAULT and an INVALID value degrades to shadow — toward LESS
write authority (the same fail-safe as ledger_shadow_config). The grader feeds
autonomy evidence into a Trap subsystem (earn-back Bayesian posteriors); shadow
logs what it WOULD fire so the operator can watch the real pattern before it
moves any autonomy math. ``off`` disables the feed entirely (the grader still
grades predictions — only the autonomy side-effect is gated).

Remaining P4 lever (B5 knob effector) lands as a sibling key when the trigger
scan ships — forward-compatible, so this file and the settings domain stay the
single source of truth.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports MODES from here, never the reverse
(one-way, the immunity rule). NO import path to ``genesis.routing`` — the
grader that reads this must keep its no-LLM lock.
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

MODES = ("off", "shadow", "live")
ARBITRATION_MODES = ("off", "shadow", "enforce")

_CONFIG_NAME = "ws2_ledger.yaml"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "autonomy_feed": "shadow",  # grader→autonomy earn-back feed; shadow-first
    "arbitration": "shadow",  # ego-proposal calibration discount; shadow-first
}


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


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
        logger.warning("ws2_ledger base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("ws2_ledger overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def autonomy_feed_mode() -> str:
    """The grader→autonomy feed mode — read live.

    Master ``enabled: false`` → ``off``. An invalid value degrades to
    ``shadow`` (observable, no write authority into the autonomy Trap — never a
    silent live-fire).
    """
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("autonomy_feed")
    if mode is False:
        # A hand-edited unquoted `autonomy_feed: off` parses as YAML-1.1
        # boolean False. That intent is unambiguous — honor it.
        return "off"
    if mode not in MODES:
        logger.warning("ws2_ledger has invalid autonomy_feed %r — degrading to shadow", mode)
        return "shadow"
    return mode


def arbitration_mode() -> str:
    """The ego-proposal arbitration-discount mode — read live.

    Master ``enabled: false`` → ``off``. An invalid value degrades to
    ``shadow`` (annotate-only — badges/escalation notes render but sort order
    never moves; the same less-authority fail-safe as ``autonomy_feed_mode``).
    """
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("arbitration")
    if mode is False:
        # Unquoted `arbitration: off` parses as YAML-1.1 boolean False.
        return "off"
    if mode not in ARBITRATION_MODES:
        logger.warning("ws2_ledger has invalid arbitration %r — degrading to shadow", mode)
        return "shadow"
    return mode
