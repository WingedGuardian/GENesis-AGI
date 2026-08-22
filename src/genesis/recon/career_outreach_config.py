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
    # module overlay). SHIPPED EMPTY so no install-specific module name lands in the
    # public repo — set it in the gitignored career_outreach.local.yaml overlay.
    # Empty (or an absent module) → the monitor no-ops cleanly.
    "reasoning_module": "",
    # Per-tick cap on auto-run DISPATCHES (bounds cost — each is a full ~timeout-long
    # run — + mirrors the engine's own daily cap). NOTE: this caps dispatches, not
    # necessarily staged drafts: a dispatch that the engine's gate refuses returns
    # verify_failed and still consumes one slot. Loud-truncation: excess targets wait
    # for the next tick, never silently dropped.
    "max_auto_runs_per_tick": 3,
    # Per-dispatch bridge timeout, in seconds. The SSH CC adapter clamps a per-call
    # ``timeout_s`` to a 3600s ceiling (``ipc.py::_MAX_TIMEOUT_CEILING``); this config
    # caps it further at 1800 (``_MAX_BY_KNOB``). The default covers the gated
    # career-agent first-touch flow (research → draft → verify → stage), MEASURED at
    # ~5.5 min live.
    "dispatch_timeout_s": 900,
    # Per-dispatch ``--max-turns`` for the auto-run. The gated flow is agentic and
    # needs far more than the ipc default of 25 (a continue-and-stage run MEASURED
    # 42 turns; a cold-start research+draft+verify+stage needs more). Passed only on
    # the heavy auto-run dispatch; the lightweight read dispatch keeps the default.
    "dispatch_max_turns": 80,
    # Overlay-only note appended to the AUTO-RUN prompt (NOT the read prompt). Ships
    # EMPTY; the install's gitignored career_outreach.local.yaml carries any
    # install-specific gate guidance (e.g. how this engine's own verification gate
    # behaves headless). Keeps install vocabulary out of the public repo.
    "autorun_note": "",
}

# Public: the settings-domain validator imports these to check knobs.
INT_KNOBS = ("max_auto_runs_per_tick", "dispatch_timeout_s", "dispatch_max_turns")

# Key-specific upper bounds — a typo (e.g. max_auto_runs_per_tick: 3000) must not
# authorize thousands of sequential draft-staging sessions, an unbounded per-run
# turn budget, or a runaway timeout. (dispatch_timeout_s has no hard external cap;
# 1800s is a generous 2x+ over the ~5.5 min measured floor, bounding a hung run.)
_MAX_BY_KNOB: dict[str, int] = {
    "max_auto_runs_per_tick": 10,
    "dispatch_timeout_s": 1800,
    "dispatch_max_turns": 200,
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
    # Master switch: require a LITERAL boolean True to stay enabled. A non-bool
    # (e.g. the string "false" from env-templated YAML, or any corruption) degrades
    # to off — the master actuator switch fails toward LESS authority.
    if cfg.get("enabled", True) is not True:
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
    """Positive-int knob with DEFAULTS fallback + a key-specific upper bound —
    config damage never zeroes a limit, crashes the monitor, or authorizes an
    unbounded run."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    cap = _MAX_BY_KNOB.get(key)
    if cap is not None and value > cap:
        logger.warning(
            "career_outreach %s=%d exceeds the max (%d) — clamping down", key, value, cap
        )
        return cap
    return value


def text_knob(cfg: dict[str, Any], key: str) -> str:
    """A string knob, damage-tolerant: a non-blank str passes; anything else
    (missing / non-str / whitespace-only) degrades to the DEFAULT for ``key``
    (``""`` when the default is empty, e.g. ``autorun_note``)."""
    value = cfg.get(key)
    if isinstance(value, str) and value.strip():
        return value
    return str(DEFAULTS.get(key, ""))


def module_name(cfg: dict[str, Any], key: str) -> str:
    """A module-name knob, damage-tolerant (non-str / blank → DEFAULTS)."""
    return text_knob(cfg, key)
