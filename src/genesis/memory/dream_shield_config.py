"""Dream-cycle importance-shield control surface — enable lever + threshold knobs.

The ONE place the dream-cycle shield consults for policy (the
``memory_integrity_config`` / ``entity_adjudication_config`` lineage). Re-reads
the merged YAML (``config/dream_shield.yaml`` + user overlay
``~/.genesis/config/dream_shield.local.yaml``) on EVERY call — no boot cache, so
an operator edit takes effect on the next scheduled run.

The shield protects high-salience memories from being consolidated away by the
dream-cycle merge path. It is enabled by default: the failure posture is to
shield MORE (an invalid percentile falls back to the shipped 0.90, never to
"shield nothing"), because a silently-off shield is exactly the blind spot this
lever exists to make operator-controllable, not accidental.

Two independent off switches:

- ``enabled: false`` in the overlay — operator intent, honored immediately.
- ``GENESIS_DREAM_SHIELD_DISABLED=1`` — scheduler-level env kill switch.

Note ``deprecated_edge_prune_days`` lives here (not a merge knob) because it is
coupled to the shield/rewire lifecycle: it bounds how long a deprecated node's
graph edges linger before the weekly link-repair prune reaps them, and MUST
exceed the manual dream-rollback review window (rollback cannot restore pruned
edges).

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only.
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

_CONFIG_NAME = "dream_shield.yaml"
_ENV_KILL_SWITCH = "GENESIS_DREAM_SHIELD_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Shield any member at/above this percentile of the collection's live
    # activation distribution (confidence × recency × access/connectivity).
    "activation_percentile": 0.90,
    # Shield any member at/above this percentile of the NONZERO betweenness-
    # centrality distribution (bridge-node protection). Degrades to
    # activation-only when the centrality cache is empty.
    "centrality_percentile": 0.90,
    # Shield any member at/above this raw confidence regardless of activation —
    # closes the rare-but-critical, low-recency gap (activation proxies
    # importance via access patterns, which breaks for high-stakes memories
    # that are rarely retrieved). Set to 0.98, deliberately ABOVE the common
    # 0.95 extraction-default spike (measured 2026-08-05: ~21% of live memories
    # sit at >=0.95 but only ~6% at >=0.98) — otherwise the floor would protect
    # a default-valued fifth of the store from ever merging, not the genuinely
    # high-confidence tail.
    "confidence_floor": 0.98,
    # Weekly link-repair prunes graph edges of nodes deprecated at least this
    # long ago. MUST exceed the dream-rollback review window (rollback cannot
    # restore pruned edges). Consumed by the link-repair phase (PR-B2).
    "deprecated_edge_prune_days": 30,
}

_INT_KNOBS = ("deprecated_edge_prune_days",)
_FLOAT01_KNOBS = ("activation_percentile", "centrality_percentile", "confidence_floor")


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
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("dream_shield base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("dream_shield overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def env_disabled() -> bool:
    """True if the scheduler-level kill switch is set."""
    return os.environ.get(_ENV_KILL_SWITCH, "").strip().lower() in ("1", "true", "yes")


def shield_enabled() -> bool:
    """Whether the shield must run — read live.

    Env kill switch or master ``enabled: false`` → off. Any other state → on
    (the shield's fail-posture is to protect MORE, never to silently skip).
    """
    if env_disabled():
        return False
    cfg = load_config()
    return bool(cfg.get("enabled", True))


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never crashes a
    job or zeroes a limit."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value


def knob_float01(cfg: dict[str, Any], key: str) -> float:
    """[0,1] float knob with DEFAULTS fallback (percentiles, confidence floor)."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return float(DEFAULTS[key])
    return float(value)
