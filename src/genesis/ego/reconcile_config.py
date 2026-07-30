"""Config lever for the ego reconcile stage (PR-5).

Cloned from ``cc.foreground_reaper_config``: fresh-read-per-call, MODES tuple +
env kill switch, degrade-toward-less-authority on damage.

The reconcile stage matches a cycle's freshly-drafted proposals against the
existing pending board (ego-scoped) plus a deterministic covered-work snapshot,
and emits per-draft verdicts (new / reaffirm / revise / withdraw-with-evidence).

Modes (increasing authority):
  - ``off``    — the reconcile stage does not run at all; drafting keeps the
    pending board in context and proposals flow straight to the realist (the
    pre-PR-5 status quo). No extra CC call.
  - ``shadow`` — reconcile runs and LOGS its verdicts, but applies NOTHING;
    drafting already goes blind (board removed from drafting context) and the
    existing realist stays as the dedup backstop. Representative observation.
  - ``live``   — shadow + APPLY: reaffirm touches ``last_validated_at``, revise
    versions the proposal in place (audit row), withdraw-with-evidence retires a
    stale pending proposal (honouring the 24h user-protection guard); the realist
    history also widens to the full ego-scoped board.

Default ``shadow``: PR-5 ships observation-only. A missing/corrupt config
degrades to DEFAULTS; an invalid ``mode`` degrades to ``shadow`` (never a silent
``live`` that applies unreviewed board mutations, never a silent ``off`` that
hides the feature). The env kill switch ``GENESIS_EGO_RECONCILE_DISABLED=1``
forces ``off`` (no reconcile CC call runs).

NOTE (PR-5 → PR-6 dependency): flipping ``mode: live`` is only safe once PR-6's
resolve-side ``expected_revision`` guards are wired into the Telegram/MCP/
dashboard resolve paths — otherwise a live revise reintroduces the approve-time
TOCTOU that versioned revision exists to close. Until then, keep ``shadow``.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``MODES`` / ``INT_KNOBS``
from here (never the reverse).
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

_CONFIG_NAME = "ego_reconcile.yaml"

_ENV_KILL_SWITCH = "GENESIS_EGO_RECONCILE_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "shadow",
    # ego_proposal_revisions retention — the reconcile stage is the first writer
    # of that (otherwise unbounded) audit table, so its prune path ships here.
    "revision_retention_days": 45,
}

# Public: the settings-domain validator imports these to check knobs.
INT_KNOBS = ("revision_retention_days",)


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
        logger.warning("ego_reconcile base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("ego_reconcile overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the reconcile stage runs under — read live.

    Env kill switch → ``off``. Master ``enabled: false`` → ``off``. An invalid
    value degrades to ``shadow`` (observe safely — never a silent ``live`` that
    applies unreviewed mutations, never a silent ``off`` that hides the feature).
    """
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return "off"
    cfg = load_config()
    if not cfg.get("enabled", True):
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # Hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
        return "off"
    if mode not in MODES:
        logger.warning("ego_reconcile has invalid mode %r — degrading to shadow", mode)
        return "shadow"
    return mode


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never zeroes a
    limit or crashes the reconcile stage."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value
