"""Config lever for rate-limit park + auto-resume.

Cloned from ``memory.entity_adjudication_config``: fresh-read-per-call, MODES
tuple + env kill switch, degrade-toward-less-authority on damage.

Failure posture: a missing/corrupt config degrades to DEFAULTS; an invalid
``mode`` degrades to ``propose_only`` (never a silent ``live``). The env kill
switch ``GENESIS_RATE_LIMIT_RESUME_DISABLED=1`` forces ``off`` regardless of the
file — an operator's emergency brake against unattended re-dispatch.

Default mode is ``off`` (DORMANT) until the resume engine lands (PR-2b): with no
resumer wired, a parked request would sit ``status='parked'`` forever while the
foreground copy promised auto-resume — recreating the very silent-death this
fixes. Shipping the park substrate off-by-default (like the reaper's dry-run /
entity-adjudication's shadow) keeps PR-2a inert-but-observable; PR-2b flips the
default to ``live``. Once ``live``: a resume only completes work whose initiation
was already user-approved (a foreground turn's typed prompt) or already
gate-approved (a direct_session's original dispatch), so it is not new autonomous
initiative — see ``rate_limit_resume`` module docstring.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``MODES`` and ``INT_KNOBS``
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

MODES = ("off", "propose_only", "live")

_CONFIG_NAME = "cc_rate_limit_resume.yaml"

_ENV_KILL_SWITCH = "GENESIS_RATE_LIMIT_RESUME_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # DORMANT until PR-2b wires the resume engine — see module docstring. PR-2b
    # flips this to "live". off → parks nothing, honest "try again later" copy.
    "mode": "off",
    # Scheduling
    "cadence_floor_minutes": 30,  # retry cadence when the reset time is unknown
    "backoff_base_minutes": 30,  # re-limit backoff base
    "backoff_cap_minutes": 240,  # re-limit backoff ceiling (4h)
    "jitter_seconds": 60,  # spread on next_attempt to avoid thundering herd
    "max_due_per_tick": 50,  # parks resumed per engine tick
    # Give-up threshold — escalate to needs_user once attempts reaches this.
    "needs_user_attempts": 40,
    # Capability pinning: a resumed conversation-origin turn runs under this
    # bounded background profile (⊆ the foreground surface). Direct-session
    # parks re-run under their own original profile.
    "conversation_resume_profile": "research",
}

# Public: the settings-domain validator imports these to check knobs.
INT_KNOBS = (
    "cadence_floor_minutes",
    "backoff_base_minutes",
    "backoff_cap_minutes",
    "jitter_seconds",
    "max_due_per_tick",
    "needs_user_attempts",
)


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
        logger.warning("cc_rate_limit_resume base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("cc_rate_limit_resume overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode auto-resume runs under — read live.

    Env kill switch → ``off``. Master ``enabled: false`` → ``off``. An invalid
    value degrades to ``propose_only`` (observable, no dispatch authority — never
    a silent ``off`` that hides the feature, never a silent ``live``).
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
        logger.warning("cc_rate_limit_resume has invalid mode %r — degrading to propose_only", mode)
        return "propose_only"
    return mode


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never zeroes a
    limit or crashes the engine."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value


def resume_profile(cfg: dict[str, Any]) -> str:
    """Bounded background profile for a resumed conversation-origin turn."""
    value = cfg.get("conversation_resume_profile")
    if isinstance(value, str) and value.strip():
        return value
    return str(DEFAULTS["conversation_resume_profile"])
