"""Memory-integrity control surface — mode lever + checker/probe knobs.

The ONE place the memory-integrity jobs consult for policy (the
repo_pulse_config lineage). Re-reads the merged YAML
(``config/memory_integrity.yaml`` + user overlay
``~/.genesis/config/memory_integrity.local.yaml``) on EVERY call — no boot
cache, so an operator edit takes effect on the next scheduled run.

Modes (``off | passive | active``):

- ``active`` (DEFAULT) — everything ``passive`` does, PLUS the Phase-1 periodic
  reconcile job (``memory/integrity_repair.py``) that drains delete-intent
  tombstones and repairs aged ghost points and lying mirrors nightly. Safe as
  the default because repair is serialized against deletes from EVERY process:
  in-process via the per-memory-id lock (``memory/_locks.py``), cross-process
  via DB-backed tombstones (``memory/delete_tombstones.py``) plus the atomic
  metadata/tombstone guard inside ``requeue_for_reembed``. Opt out with
  ``mode: passive`` in the local overlay.
- ``passive`` — run the read-only checks and surface findings (persist +
  posture alert + dashboard). Never repairs. This was the whole of Phase 0.
- ``off`` — do not run at all.

Failure posture: an INVALID mode degrades to ``passive`` (still observing, no
repair) — never to ``off``, because a silently-not-running integrity checker is
itself the blind spot this subsystem exists to remove. Missing/corrupt config
degrades layer-by-layer to DEFAULTS. The scheduler-level kill switch is
separate: ``GENESIS_MEMORY_INTEGRITY_DISABLED=1`` skips job registration
entirely.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports MODES from here, never the reverse.
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

MODES = ("off", "passive", "active")

_CONFIG_NAME = "memory_integrity.yaml"
_ENV_KILL_SWITCH = "GENESIS_MEMORY_INTEGRITY_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Repair on by default: delete-vs-repair is serialized in-process by the
    # per-memory-id lock (memory/_locks.py) and cross-process by delete-intent
    # tombstones (memory/delete_tombstones.py) + the atomic requeue guard
    # (crud/pending_embeddings.py). Opt out with mode: passive in the overlay.
    "mode": "active",
    # ── consistency checker ──
    "sample_fraction": 1.0,  # 1.0 = exact full scan (cheap at single-user scale)
    "max_points": 500_000,  # Qdrant scroll budget; exceeding it sets truncated
    "severe_min_count": 5,  # lying_mirror + fts_invisible floor → degraded
    "pollution_min_count": 50,  # ghost/unexpected/deprecated floor
    "pollution_fraction": 0.01,  # or this fraction of the corpus, whichever higher
    "max_offender_sample": 25,  # capped offender ids per class in the report
    # ── recall-health probe ──
    "probe_limit": 10,  # results retrieved per golden query
    "max_probes_per_run": 25,  # cap golden cases exercised per run
    "rerank": True,  # exercise the real rerank stage
    "rerank_timeout_s": 10.0,  # wall-clock bound on rerank per query
    "min_golden_for_status": 5,  # below this the run is 'unknown' (needs setup)
    "baseline_window_runs": 7,  # trailing runs averaged for the drift baseline
    "baseline_min_runs": 3,  # below this: observation period, no drift verdict
    "drift_band": 0.2,  # degraded if baseline_hit_rate - hit_rate > this
    # ── reconcile repair lane (Phase 1, mode=active only) ──
    "repair_min_age_seconds": 3600,  # never touch an offender younger than this
    "max_repairs_per_run": 500,  # per-run work cap; hitting it sets capped=1
    # ── shared ──
    "stale_report_days": 3,  # no non-unknown report within → staleness alert
    "retention_days": 90,  # prune reports/probe/reconcile runs older than this
}

_INT_KNOBS = (
    "max_points",
    "severe_min_count",
    "pollution_min_count",
    "max_offender_sample",
    "probe_limit",
    "max_probes_per_run",
    "min_golden_for_status",
    "baseline_window_runs",
    "baseline_min_runs",
    "repair_min_age_seconds",
    "max_repairs_per_run",
    "stale_report_days",
    "retention_days",
)

_FLOAT01_KNOBS = ("sample_fraction", "pollution_fraction", "drift_band")


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
        logger.warning("memory_integrity base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("memory_integrity overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def env_disabled() -> bool:
    """True if the scheduler-level kill switch is set."""
    return os.environ.get(_ENV_KILL_SWITCH, "").strip().lower() in ("1", "true", "yes")


def effective_mode() -> str:
    """The mode the jobs must run under — read live.

    Env kill switch or master ``enabled: false`` → ``off``. An invalid value
    degrades to ``passive`` — still observing but never repairing (toward less
    write authority), and never a silent ``off``.
    """
    if env_disabled():
        return "off"
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # YAML-1.1 unquoted `mode: off` → boolean False. Honor the intent.
        return "off"
    if mode not in MODES:
        logger.warning("memory_integrity has invalid mode %r — degrading to passive", mode)
        return "passive"
    return mode


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never crashes a
    job or zeroes a limit."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value


def knob_float01(cfg: dict[str, Any], key: str) -> float:
    """[0,1] float knob with DEFAULTS fallback."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        return float(DEFAULTS[key])
    return float(value)


def knob_float(cfg: dict[str, Any], key: str) -> float:
    """Positive-float knob with DEFAULTS fallback (for unbounded values like
    ``rerank_timeout_s``)."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return float(DEFAULTS[key])
    return float(value)
