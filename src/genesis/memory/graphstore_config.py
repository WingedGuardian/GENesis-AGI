"""Which GraphStore answers memory-graph reads.

Failure posture, stated up front because every branch here degrades in the same
direction: anything unreadable, unrecognised, or unset selects ``networkx`` --
the store that has always answered. This lever can only ever move reads ONTO a
new backend deliberately; it can never strand them on one by accident.

That asymmetry is the whole point. The FalkorDB store is faster to keep current
(no rebuild) but depends on a running server, a projection, and a client
library. NetworkX depends on nothing but the process it lives in. When in
doubt, the fewer moving parts win.

Dependency direction matches its sibling `entity_adjudication_config`: stdlib +
yaml + `genesis.env` + `genesis._config_overlay` only. `mcp.health.settings`
imports MODES from here, never the reverse.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import local_overlay_mtime, merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)

#: networkx is first and is the default: it is the incumbent, and the degrade
#: target for every unreadable or unrecognised value.
MODES = ("networkx", "falkordb")

_CONFIG_NAME = "graphstore.yaml"

_ENV_KILL_SWITCH = "GENESIS_FALKORDB_STORE_DISABLED"

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "networkx",
}


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


#: (base mtime, overlay mtime) -> merged config. Keyed on the files themselves,
#: so there is no TTL and no staleness window: a hand edit or a
#: settings_update takes effect on the very next call, exactly as the
#: fresh-read did.
_CACHE: tuple[tuple[float, float], dict[str, Any]] | None = None


def _config_mtimes() -> tuple[float, float]:
    """Freshness key for `load_config`'s cache.

    Follows the house pattern (`channels/tts_config.py`,
    `perception/confidence.py`) but keeps the two mtimes as a TUPLE rather than
    summing them: a sum can collide when one file moves forward by exactly what
    the other moves back, which a backdated write can produce.
    """
    base_path = _base_path()
    try:
        base_mtime = base_path.stat().st_mtime
    except OSError:
        base_mtime = 0.0  # absent base is a legitimate state — DEFAULTS answer
    return (base_mtime, local_overlay_mtime(base_path))


def reset_config_cache() -> None:
    """Drop the cache. For tests, which write configs faster than mtime resolves."""
    global _CACHE
    _CACHE = None


def load_config() -> dict[str, Any]:
    """The merged config, re-parsed only when a file on disk actually changed.

    Two stat() calls guard the parse. MEASURED 2026-09-07: the parse is ~1.8ms
    and a stat is ~8us, so the guard costs 0.4% of what it saves. That matters
    because `effective_mode()` is called once per recall RESULT inside the loop
    at `mcp/memory/core.py:452-457`, and the uncached read cost roughly as much
    as the NetworkX traversal it selects -- while being charged to `query_ms`
    and drawn from the 500ms graph budget, so it reduced how many memories got
    graph neighbours at all. On the DEFAULT path, for a backend not selected.

    Correcting the rationale this docstring used to give for having no cache: it
    claimed a cache would defeat `dream_centrality` picking up a mode change on
    its next run. That reasoning named the one caller it cannot apply to --
    `centrality_scores` is pinned to the NetworkX store and never consults this
    lever at all. Freshness is preserved anyway, by mtime rather than by
    re-parsing blind.
    """
    global _CACHE
    stamps = _config_mtimes()
    if _CACHE is not None and _CACHE[0] == stamps:
        return copy.deepcopy(_CACHE[1])
    merged = _load_uncached()
    _CACHE = (stamps, merged)
    return copy.deepcopy(merged)


def _load_uncached() -> dict[str, Any]:
    """The actual read + overlay merge. Unconditional; callers use `load_config`."""
    merged = copy.deepcopy(DEFAULTS)
    base_path = _base_path()
    base: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        logger.warning("graphstore base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("graphstore overlay merge failed", exc_info=True)
    merged.update(base)
    return merged


def effective_mode() -> str:
    """The store to use: ``networkx`` or ``falkordb``.

    Three ways to land on networkx, in precedence order: the env kill switch,
    `enabled` being anything other than exactly True, or anything the file says
    that is not a recognised mode.
    """
    if os.environ.get(_ENV_KILL_SWITCH) == "1":
        return "networkx"
    cfg = load_config()
    # `is not True`, not falsiness. A hand-edited overlay is YAML, and a QUOTED
    # `enabled: "false"` parses as a non-empty string, which is truthy — so a
    # falsiness test reads an operator's attempt to DISABLE the backend as
    # permission to enable it, which is both the wrong answer and the wrong fail
    # direction for a module whose whole posture is degrade-toward-networkx.
    # Only the settings API type-checks this; the file does not, and the file is
    # the surface a person edits. Every non-True value now degrades, including
    # `1` and `"true"` — narrower than YAML's own boolean set on purpose: a
    # value we cannot read as exactly True is a value we decline to act on.
    if cfg.get("enabled", True) is not True:
        return "networkx"
    mode = cfg.get("mode")
    if mode is False:
        # A hand-edited unquoted `mode: off` parses as YAML-1.1 boolean False.
        # Not a mode here, but the shape is worth catching explicitly rather
        # than letting it fall through the generic branch with a confusing log.
        logger.warning("graphstore mode is boolean False — degrading to networkx")
        return "networkx"
    if mode not in MODES:
        logger.warning("graphstore has invalid mode %r — degrading to networkx", mode)
        return "networkx"
    return str(mode)
