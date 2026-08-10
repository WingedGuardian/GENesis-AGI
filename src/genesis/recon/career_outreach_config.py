"""Config lever for the career-outreach monitor (the "career outreach driver").

Cloned from ``recon.github_steward_config``: fresh-read-per-call, ``MODES``
tuple + env kill switch, degrade-toward-less-authority on damage.

This monitor is an ACTUATOR-in-recon (not the no-side-effect sense-and-report
recon contract): on a daily cadence it drives a configured external career-agent
module (the install's own "hands", declared in ``~/.genesis/config/modules/``) to
run job-outreach discovery and stage first-touch DRAFTS, then nudges the owner to
review + send. It NEVER sends mail itself — the external module stages drafts into
the owner's mail Drafts and the owner clicks Send (draft-and-hold). The autonomous
act is bounded to discover+draft (reversible: a bad draft is deleted, not
un-sent); the standing ``live`` lever IS the owner's authorization for that
bounded loop.

Modes (increasing authority):
  - ``off``     — the monitor does not run. SHIPPED DEFAULT: an actuator does
    nothing on a fresh install until the owner opts in.
  - ``observe`` — resolve state + seed the already-nudged set, but NEVER dispatch
    a draft-staging run and NEVER nudge. The seed step prevents a cold-start nudge
    over a pre-existing draft backlog; the operator flips ``off → observe → live``.
  - ``live``    — drive due discovery + up to ``max_auto_runs_per_tick``
    draft-staging runs, then push ONE owner nudge when NEW drafts were staged.

A missing/corrupt config degrades to DEFAULTS (mode ``off``); an invalid ``mode``
value degrades to ``observe`` (record/seed safely, never a silent authority
grant, never hide the feature entirely). Env kill switch
``GENESIS_CAREER_OUTREACH_DISABLED=1`` forces ``off``.

Generalizability: the career-agent modules are the install's own overlay
modules — NO install-specific targeting data ships here. On an install without
those modules the monitor no-ops cleanly (the module registry returns ``None``).

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

_CONFIG_NAME = "career_outreach.yaml"

_ENV_KILL_SWITCH = "GENESIS_CAREER_OUTREACH_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # Shipped default: OFF. This monitor drives an external engine to act on the
    # owner's behalf — it stays inert until the owner deliberately opts in.
    "mode": "off",
    # Name of the external reasoning module (as declared in the install's ~/.genesis
    # module overlay). An absent module → the monitor no-ops cleanly. Config knob,
    # never a hardcoded literal elsewhere.
    "reasoning_module": "Career Ops",  # SSH CC dispatch — runs the outreach modes
    # Per-tick cap on draft-staging auto-runs (bounds cost + mirrors the remote
    # engine's own daily cap). Loud-truncation: excess page-worthy accounts wait
    # for the next tick, never silently dropped.
    "max_auto_runs_per_tick": 3,
    # Per-dispatch bridge timeout, in seconds. MUST stay under the module's own
    # SSH CC hard cap (300s) so a slow run is our timeout, cleanly, not the SSH
    # adapter's opaque one.
    "dispatch_timeout_s": 240,
}

# Public: the settings-domain validator imports these to check knobs.
INT_KNOBS = ("max_auto_runs_per_tick", "dispatch_timeout_s")


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
        logger.warning("career_outreach base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("career_outreach overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The mode the monitor runs under — read live.

    Env kill switch → ``off``. Master ``enabled: false`` → ``off``. An invalid
    value degrades to ``observe`` (record/seed safely, never act, never a silent
    ``off`` that hides the feature).
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
        logger.warning("career_outreach has invalid mode %r — degrading to observe", mode)
        return "observe"
    return mode


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never zeroes a
    limit or crashes the monitor."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value


def module_name(cfg: dict[str, Any], key: str) -> str:
    """A module-name knob, damage-tolerant (non-str / blank → DEFAULTS)."""
    value = cfg.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return str(DEFAULTS[key])
