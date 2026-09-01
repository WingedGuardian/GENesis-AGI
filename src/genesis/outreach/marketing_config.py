"""Config lever for the autonomous COLD marketing-outreach substrate.

Cloned from ``recon.career_outreach_config`` / ``autonomy.contributor_worklog_config``:
fresh-read-per-call, ``MODES`` tuple + env kill switch, degrade-toward-less-authority
on damage.

This lever gates the ``marketing_send`` MCP tool — the only path that stages a
cold marketing email for a curated ``marketing_prospects`` recipient. It is NOT
the autonomous send authorization: every marketing send still converges on the
WS-8 email autonomy gate (``outreach.pipeline._deliver`` → ``EmailAutonomyGate``),
whose BULK capability cell ships at ASK (holds everything for owner approval).
This lever is the OUTER off-switch: with ``off`` the tool refuses before it ever
resolves a recipient or enqueues.

Modes (increasing authority):
  - ``off``     — the tool refuses to stage any marketing send. SHIPPED DEFAULT:
    the cold-send substrate does nothing until the owner opts in.
  - ``observe`` — the tool is reachable and resolves/validates the recipient, but
    the send is still gated below by the email autonomy gate (BULK cell at ASK →
    held). In practice, while the BULK cell is ungranted, ``observe`` and ``live``
    behave identically at the wire (everything holds); the distinction is the
    owner's stated posture and lands once the BULK cell can graduate.
  - ``live``    — same wire behavior today (the gate still authorizes each send),
    but the owner has affirmatively enabled autonomous cold outreach.

A missing/corrupt config degrades to DEFAULTS (mode ``off``). An invalid ``mode``
value degrades to ``off`` — for a cold-send substrate the safest degrade is the
LEAST authority (fully inert), not a partial-authority fallback. Env kill switch
``GENESIS_MARKETING_OUTREACH_DISABLED=1`` forces ``off``.

Generalizability: ships ZERO install-specific targeting data. Prospects are
owner-curated rows in ``marketing_prospects`` (private DB), never in this file or
the public repo. On a fresh clone the store is empty and the tool cleanly refuses.

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

MODES = ("off", "observe", "live")

_CONFIG_NAME = "marketing_outreach.yaml"

_ENV_KILL_SWITCH = "GENESIS_MARKETING_OUTREACH_DISABLED"

DEFAULTS: dict[str, Any] = {
    # Master switch: require a LITERAL boolean True to stay enabled.
    "enabled": True,
    # Shipped default: OFF. This substrate stages cold outreach on the owner's
    # behalf — it stays inert until the owner deliberately opts in.
    "mode": "off",
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
        logger.warning("marketing_outreach base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("marketing_outreach overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the marketing-send substrate runs under — read live.

    Env kill switch → ``off``. Master ``enabled: false`` → ``off``. An invalid
    value degrades to ``off`` (least authority — a cold-send actuator must never
    fail toward MORE send capability).
    """
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return "off"
    cfg = load_config()
    # Master switch: require a LITERAL boolean True to stay enabled. A non-bool
    # (e.g. the string "false" from env-templated YAML, or any corruption)
    # degrades to off — the master actuator switch fails toward LESS authority.
    if cfg.get("enabled", True) is not True:
        return "off"
    mode = cfg.get("mode")
    if mode is False:
        # Hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
        return "off"
    if mode not in MODES:
        logger.warning("marketing_outreach has invalid mode %r — degrading to off", mode)
        return "off"
    return mode
