"""Config lever for the undisposed-ledger escalation sweep (learning scheduler).

Cloned from ``awareness.follow_up_watchdog_config`` — same shape: an ``enabled``
master switch, positive-int knobs, an alert/priority enum, and an env kill
switch. The one addition is ``escalate_added_by``, a provenance allow-list (see
below).

Failure posture: a missing/corrupt config degrades to DEFAULTS; the env kill
switch ``GENESIS_LEDGER_ESCALATION_DISABLED=1`` forces the sweep off regardless
of the file. Unlike the watchdog this module's consumer WRITES (it creates
follow-ups), so every degrade is toward LESS write authority — a damaged knob
falls back to the default rather than to "unbounded".

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports the public ``DEFAULTS`` / ``INT_KNOBS``
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
from genesis.db.crud.session_charters import VALID_ADDED_BY
from genesis.env import repo_root

logger = logging.getLogger(__name__)

_CONFIG_NAME = "ledger_escalation.yaml"
_ENV_KILL_SWITCH = "GENESIS_LEDGER_ESCALATION_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # A ledger row must be untouched (updated_at, else created_at) this long
    # before it can escalate. Any ledger_update bumps updated_at, so a row
    # someone is still working restarts the clock.
    "stale_days": 5,
    # AND the owning session must have been quiet this long. Both thresholds
    # must pass: a stale row in a LIVE session is that session's to dispose, and
    # escalating it would take the decision away from the one party still able
    # to make it. Measured 2026-09-06 on the live ledger: the double threshold
    # correctly held back 35 of 50 open rows, including four rows 5.9-6.8d
    # untouched whose session had prompted seconds earlier.
    "quiet_days": 5,
    # Escalations CREATED per run. The sweep is hourly, so a backlog drains at
    # this rate rather than arriving at once; deferred ids are logged and
    # returned, never silently dropped. Draining depends on the sweep filtering
    # already-escalated rows BEFORE applying this cap — see the comment at that
    # loop; the reverse order starves the backlog permanently.
    # It caps WRITES, not work: every candidate is still stat'd for liveness and
    # every terminal row still hashed during reconcile, before this applies. Free
    # at real ledger sizes (181 rows on the install this was built against) and
    # bounded by the tripwires in `session_charters`, but a ledger orders of
    # magnitude larger would make the hourly tick expensive regardless of it.
    "max_per_run": 5,
    # Priority of the created follow-up.
    "priority": "high",
    # Provenance allow-list: which `session_ledger.added_by` values may escalate.
    # Default is foreground-only ON PURPOSE. `ambient_ledger_extractor` rows are
    # the detached extractor's PROPOSALS, not agreements a human made, so
    # escalating them would ask the owner to dispose of something nobody
    # committed to. The extractor shipped OFF (the ambient-extractor migration,
    # #1541) and has written 0 live rows here, but the schema CHECK now admits
    # the value, so this allow-list ships with the sweep rather than after the
    # first flood. Widen it deliberately if extractor rows ever become
    # agreements.
    "escalate_added_by": ["foreground"],
}

INT_KNOBS = ("stale_days", "quiet_days", "max_per_run")
_VALID_PRIORITY = ("low", "medium", "high", "critical")


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the merged config fresh — per call, NO cache.

    Deep-merges (defaults <- base yaml <- .local.yaml overlay). Missing or
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
        logger.warning("ledger_escalation base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("ledger_escalation overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def is_enabled() -> bool:
    """True unless the env kill switch is set or the config disables it."""
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return False
    return bool(load_config().get("enabled", True))


def knob_int(cfg: dict[str, Any], key: str) -> int:
    """Positive-int knob with DEFAULTS fallback — config damage never zeroes a
    limit or crashes the sweep."""
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return int(DEFAULTS[key])
    return value


def priority(cfg: dict[str, Any]) -> str:
    """The configured follow-up priority, falling back on damage."""
    value = cfg.get("priority")
    if value in _VALID_PRIORITY:
        return str(value)
    return str(DEFAULTS["priority"])


def escalate_added_by(cfg: dict[str, Any]) -> frozenset[str]:
    """The `added_by` values allowed to escalate.

    Damage degrades to the DEFAULT (foreground-only), never to "everything":
    this gates who may create follow-ups, so a corrupt value must not widen
    write authority. Unknown values are dropped rather than passed through to
    the SQL — they can only ever match nothing, and silently querying for a
    value the schema CHECK forbids would read as "no rows escalate" for a
    reason no operator could see.
    """
    value = cfg.get("escalate_added_by")
    if not isinstance(value, list) or not value:
        return frozenset(DEFAULTS["escalate_added_by"])
    allowed = {v for v in value if isinstance(v, str) and v in VALID_ADDED_BY}
    if not allowed:
        logger.warning(
            "ledger_escalation: escalate_added_by named no valid provenance "
            "(got %r; valid: %s) — falling back to %s",
            value,
            sorted(VALID_ADDED_BY),
            DEFAULTS["escalate_added_by"],
        )
        return frozenset(DEFAULTS["escalate_added_by"])
    return frozenset(allowed)
