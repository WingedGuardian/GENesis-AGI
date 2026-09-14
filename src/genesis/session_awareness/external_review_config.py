"""External-review control surface — mode lever, dispatch budget, orchestrator.

The ONE place the external-review runner consults for policy
(``repo_pulse_config`` lineage, same overlay mechanics):

- :func:`effective_mode` — ``off | dry_run | live``, re-read from the merged YAML
  (``config/external_review.yaml`` + the user overlay
  ``~/.genesis/config/external_review.local.yaml``) on EVERY call. No boot cache —
  each timer-fired runner is a fresh process anyway.

WHAT AN "EXTERNAL REVIEW ORCHESTRATOR" IS HERE
==============================================
A separately-installed tool that runs a review over a pull request and posts its
report as a PR comment. This repo ships only the ADAPTER — the policy, the
dedup, the budget and the audit trail. It ships NO orchestrator, names none as a
default, and assumes nothing about which one an install uses; the binary, its
argv shape and the marker it stamps its report with are install-local
configuration, because they are properties of a tool that lives outside this
repository.

The shipped default is therefore EMPTY, and an unconfigured install is a clean
no-op rather than a broken one. That is the same reasoning the repo applies to
every other optional dependency: presence is never assumed.

WHY A BUDGET IS A FIRST-CLASS KNOB, not a tuning detail
=======================================================
A review of this kind spawns a fleet of agent sessions and runs for minutes.
MEASURED on the first live run (2026-09-14): one review moved the 5-hour
subscription window from 12% to 15% — roughly 3% of the window for a single pull
request, on a window every FOREGROUND session shares. A busy repository opens
15-27 pull requests a day. An unbudgeted scan would exhaust that pool, which is
why ``max_dispatches_per_scan`` defaults to 1 and the runner treats it as a hard
cap rather than a target: the timer's cadence, not the queue's depth, sets the
rate.

WHY ``allow_workflows`` IS FAIL-CLOSED AND EMPTY
================================================
The autonomous path dispatches without passing through the autonomous-CLI
approval gate (see the runner's docstring). What stands in for that gate is
SCOPE: only workflows an operator has explicitly named may be dispatched, and
the shipped list is empty, so nothing runs until someone declares what may. A
typo can therefore only ever narrow the scope, never widen it, and an install
cannot silently acquire the ability to dispatch a workflow that writes code.

FAILURE POSTURE
===============
A missing or corrupt config degrades to DEFAULTS; an INVALID mode degrades to
``dry_run`` — less authority than ``live``, but never silently ``off``, because a
runner that quietly stops reviewing looks exactly like a queue with nothing to
review. The kill switch is separate and stdlib-cheap:
``GENESIS_EXTERNAL_REVIEW_DISABLED=1`` stops the runner before it reads YAML at
all, so a wedged config can never keep it running.

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

#: ``off`` never dispatches. ``dry_run`` decides and records, but spawns nothing —
#: the observation mode. ``live`` dispatches.
MODES = ("off", "dry_run", "live")

#: An unreadable or unrecognised mode lands here: visible, recorded, no spend.
_DEGRADED_MODE = "dry_run"

#: The env kill switch, checked BEFORE any config read so a broken overlay cannot
#: keep the runner alive.
DISABLE_ENV = "GENESIS_EXTERNAL_REVIEW_DISABLED"

_CONFIG_BASENAME = "external_review.yaml"

DEFAULTS: dict[str, Any] = {
    "mode": "live",
    # EMPTY BY DESIGN — see the module docstring. An install names its own
    # orchestrator in the overlay; a fresh clone dispatches nothing.
    "orchestrator": {
        # The executable. Resolved on PATH, or given as an absolute path.
        "command": "",
        # argv AFTER the command. `{workflow}` and `{pr}` are substituted.
        "argv": [],
        # Which workflow to run, and the closed set it must belong to.
        "workflow": "",
        "allow_workflows": [],
        # The HTML marker the orchestrator stamps its own report with, used to
        # find that report among a PR's comments. Without it there is no way to
        # tell an already-reviewed head from a new one, so the runner refuses to
        # dispatch rather than risk repeating every review on every tick.
        "report_marker": "",
    },
    # Hard cap per scan, not a target. See the budget rationale above.
    "max_dispatches_per_scan": 1,
    # A draft is still being written; reviewing it spends a fleet on a moving target.
    "skip_drafts": True,
    # A red or still-running suite means the author is not asking for review yet.
    "require_ci_green": True,
}


def _config_path() -> Path:
    """The shipped config. The overlay is NOT resolved here: ``merge_local_overlay``
    derives ``~/.genesis/config/<stem>.local.yaml`` from this path itself, and
    re-deriving it at the call site is the drift that put the same rule in five
    places elsewhere in this repo."""
    return Path(repo_root()) / "config" / _CONFIG_BASENAME


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Layers defaults ← shipped yaml ← ``.local.yaml`` overlay, degrading
    layer-by-layer toward DEFAULTS. Never raises.
    """
    merged = copy.deepcopy(DEFAULTS)
    base: dict[str, Any] = {}
    base_path = _config_path()
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001 — config must never break the runner
        logger.warning("external_review base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:  # noqa: BLE001
        logger.warning("external_review overlay merge failed", exc_info=True)

    # The orchestrator block is merged KEY-BY-KEY rather than replaced wholesale:
    # an overlay that sets only `command` must not blank the sibling keys it did
    # not mention, which a plain dict.update would do.
    orch = copy.deepcopy(DEFAULTS["orchestrator"])
    incoming = base.pop("orchestrator", None)
    if isinstance(incoming, dict):
        orch.update(incoming)
    merged.update(base)
    merged["orchestrator"] = orch
    return merged


def disabled_by_env() -> bool:
    """True when the stdlib-cheap kill switch is set.

    Checked before any config read, so an unparseable overlay can never leave the
    runner dispatching.

    Accepts the spellings an operator actually reaches for. Honouring only ``"1"``
    means ``true``/``yes``/``on`` read as NOT SET and the runner keeps dispatching —
    a kill switch that fails open on a plausible spelling, which is the one direction
    a kill switch must never fail.
    """
    return os.environ.get(DISABLE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def effective_mode(config: dict[str, Any] | None = None) -> str:
    """``off`` | ``dry_run`` | ``live``, with the kill switch outranking config."""
    if disabled_by_env():
        return "off"
    cfg = load_config() if config is None else config
    mode = cfg.get("mode")
    if mode in MODES:
        return str(mode)
    logger.warning(
        "external_review: mode %r is not one of %s; degrading to %s",
        mode,
        MODES,
        _DEGRADED_MODE,
    )
    return _DEGRADED_MODE


def max_dispatches_per_scan(config: dict[str, Any] | None = None) -> int:
    """The per-scan hard cap, coerced to a sane floor.

    A non-integer, negative or absent value degrades to the DEFAULT rather than to
    zero: zero would silently stop all reviewing, which is the one failure this
    knob must not produce by accident. An explicit ``0`` is honoured — that is what
    ``mode: off`` is for, but an operator who writes it means it.
    """
    cfg = load_config() if config is None else config
    raw = cfg.get("max_dispatches_per_scan", DEFAULTS["max_dispatches_per_scan"])
    if isinstance(raw, bool) or not isinstance(raw, int):
        logger.warning(
            "external_review: max_dispatches_per_scan %r is not an integer; using %s",
            raw,
            DEFAULTS["max_dispatches_per_scan"],
        )
        return int(DEFAULTS["max_dispatches_per_scan"])
    if raw < 0:
        logger.warning(
            "external_review: max_dispatches_per_scan %r is negative; using %s",
            raw,
            DEFAULTS["max_dispatches_per_scan"],
        )
        return int(DEFAULTS["max_dispatches_per_scan"])
    return raw


def orchestrator(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """The install's orchestrator block, with every key present."""
    cfg = load_config() if config is None else config
    block = cfg.get("orchestrator")
    if not isinstance(block, dict):
        return copy.deepcopy(DEFAULTS["orchestrator"])
    merged = copy.deepcopy(DEFAULTS["orchestrator"])
    merged.update(block)
    return merged


def workflow_name(config: dict[str, Any] | None = None) -> str | None:
    """The workflow to dispatch, or ``None`` when it is not permitted.

    Validated against the install's own ``allow_workflows`` rather than accepted
    verbatim: that closed set is what stands in for the autonomous-CLI approval
    gate this path does not pass through, so a config typo must be able to narrow
    the scope and never to widen it. An EMPTY allowlist permits nothing, which is
    what makes an unconfigured install safe by construction.
    """
    block = orchestrator(config)
    name = block.get("workflow") or ""
    allowed = block.get("allow_workflows") or []
    if not isinstance(allowed, list) or not allowed:
        logger.warning("external_review: no allow_workflows configured — nothing may be dispatched")
        return None
    if name in allowed:
        return str(name)
    logger.warning(
        "external_review: workflow %r is not in allow_workflows %s; refusing",
        name,
        sorted(str(a) for a in allowed),
    )
    return None
