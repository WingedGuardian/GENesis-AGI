"""Which GraphStore answers memory-graph reads.

Failure posture, stated up front because every branch here degrades in the same
direction: anything unrecognised (a mode that is not one of ``MODES``, an
``enabled`` that is not exactly True, the env kill switch) selects ``networkx``
-- the store that depends on nothing. The SHIPPED default
(``config/graphstore.yaml``, mirrored by ``DEFAULTS``) is ``falkordb``, and an
unreadable overlay is ignored as if absent.

The where-present guard (``selection``): when ``falkordb`` comes only from the
shipped file -- no install overlay sets ``mode`` -- and this install has no
engine unit rendered, reads stay on ``networkx`` QUIETLY. That is an install the
engine was never provisioned on, not a fallback. An overlay that sets
``mode: falkordb`` is a deliberate choice and stays loud, and so does a unit
that exists but is stopped or failed.

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

from genesis._config_overlay import local_overlay_key, merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)

#: networkx is first: it is the degrade target for every unreadable or
#: unrecognised value. The shipped default is falkordb (config/graphstore.yaml).
MODES = ("networkx", "falkordb")

#: ``quiet_reason`` for a shipped-default falkordb selection on an install with
#: no engine unit: reads stay on networkx, and the cutover telemetry records the
#: row as neither a fallback nor a break.
ENGINE_NOT_PROVISIONED = "engine_not_provisioned"

#: The engine's unit, as bootstrap.sh and install.sh render it.
_ENGINE_UNIT = "genesis-falkordb.service"

_CONFIG_NAME = "graphstore.yaml"

_ENV_KILL_SWITCH = "GENESIS_FALKORDB_STORE_DISABLED"

#: Match config/graphstore.yaml (a test locks the two together), so an install
#: whose base file fails to read gets the shipped intent, not a second opinion.
#: The where-present guard still applies on top of it.
DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "mode": "falkordb",
}


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def engine_unit_path() -> Path:
    """Where the installers render the engine's unit: ``$SYSTEMD_USER_DIR``,
    which bootstrap.sh and install.sh both set to ``$HOME/.config/systemd/user``
    (resolved from the home directory, as ``infra_profile``'s falkordb collector
    does). Rendered only from an engine verified on this install, so its absence
    means the engine was never provisioned here."""
    return Path.home() / ".config" / "systemd" / "user" / _ENGINE_UNIT


def engine_unit_present() -> bool:
    """Whether the engine's unit is rendered AND enabled on this install.

    The unit file alone is not enough: installs before the shipped default
    changed rendered it and left it disabled, and an operator opts out by
    disabling it. So the unit counts only with its ``default.target.wants``
    link (the template's ``WantedBy``), which ``systemctl enable`` creates and
    ``disable`` removes. A disabled or opted-out engine reads as not
    provisioned, and the guard keeps reads quietly on networkx.

    Not cached behind a key, deliberately: the key would itself be a ``stat()``
    of this path, and a stat is the whole check. So a unit that appears (a
    bootstrap run) or disappears is seen on the very next call, the same
    freshness the config cache gives the lever files. A path that cannot be
    inspected reads as present, so the guard never goes quiet on a state it
    could not observe.
    """
    try:
        unit = engine_unit_path()
        wants = unit.parent / "default.target.wants" / _ENGINE_UNIT
        return unit.is_file() and (wants.is_symlink() or wants.exists())
    except OSError:
        return True


#: (base ns, base size, overlay ns, overlay size) -> (merged config, whether the
#: overlay itself sets ``mode``). Keyed on the files themselves, so there is no
#: TTL: a hand edit or a settings_update takes effect on the very next call,
#: exactly as the fresh read did. Nanoseconds and size rather than a float
#: mtime, because a rewrite inside one filesystem tick is invisible to the
#: coarser key and would pin the old value indefinitely.
_CACHE: tuple[tuple[Any, ...], dict[str, Any], bool] | None = None


def _config_mtimes() -> tuple[Any, ...]:
    """Freshness key for `load_config`'s cache.

    Follows the house pattern (`channels/tts_config.py`,
    `perception/confidence.py`) but keeps the parts as a TUPLE rather than
    summing them: a sum can collide when one file moves forward by exactly what
    the other moves back, which a backdated write can produce.

    NANOSECONDS AND SIZE, not a float mtime alone. `st_mtime` is a float whose
    resolution is the filesystem's, and a rewrite that lands inside one tick —
    a coarse filesystem, a fast `settings_update`, an editor or a restore that
    preserves timestamps — leaves the key unchanged, so the cache would serve
    the OLD mode indefinitely against a file that had actually changed. That is
    the failure this cache must not have: the module's whole contract is that a
    lever change takes effect on the next traversal. `st_mtime_ns` is an integer
    at nanosecond resolution and `st_size` moves on almost any real edit; both
    come from the same `stat()` already being made, so the guard costs nothing
    it did not already cost.

    Still not a content hash, deliberately — hashing would re-read the file,
    which is the work the cache exists to avoid. This narrows the window rather
    than closing it, and `reset_config_cache()` is the deterministic escape for
    a caller that knows it just wrote.
    """
    base_path = _base_path()
    try:
        st = base_path.stat()
        base_key: tuple[Any, ...] = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        base_key = (0, 0, 0)  # absent base is a legitimate state — DEFAULTS answer
    # The overlay is the file `settings_update` writes, so it is the one whose
    # same-tick rewrite matters most. `local_overlay_key` is the shared helper's
    # finer form; absent overlay answers (0, 0), which a real file cannot.
    return base_key + local_overlay_key(base_path)


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
    return copy.deepcopy(_cached()[0])


def mode_set_by_overlay() -> bool:
    """Whether the install overlay ITSELF sets ``mode``.

    The merged config cannot say where its ``mode`` came from, so the overlay is
    read alone, through the same ``merge_local_overlay`` resolution the merge
    uses (user config dir first, then the repo sibling), and cached on the same
    key as ``load_config``. An unreadable overlay sets nothing, as in the merge.
    """
    return _cached()[1]


def _cached() -> tuple[dict[str, Any], bool]:
    global _CACHE
    stamps = _config_mtimes()
    if _CACHE is not None and _CACHE[0] == stamps:
        return _CACHE[1], _CACHE[2]
    merged = _load_uncached()
    try:
        overlay_mode = "mode" in merge_local_overlay({}, _base_path())
    except Exception:
        overlay_mode = False
    _CACHE = (stamps, merged, overlay_mode)
    return merged, overlay_mode


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


def config_is_readable() -> bool:
    """Did the graphstore config parse cleanly? ``False`` means it did not.

    `load_config()` deliberately ABSORBS a parse failure and returns DEFAULTS,
    because every read path here degrades toward networkx and a broken file
    must never take reads down with it. That is right for READERS and wrong for
    anything deciding whether to ACT: `DEFAULTS["enabled"]` is True, so a
    corrupted overlay looks exactly like an operator who enabled the backend.

    MEASURED consequence before this existed: an operator corrupting
    `graphstore.local.yaml` while trying to write `enabled: false` got the
    defaults, and the hourly projector read that as consent and kept rebuilding
    — the operator's intent inverted by a typo, silently.

    So this answers the question `load_config` cannot: not "what is the
    config" but "do we actually know what the config is". Callers that only
    READ can keep ignoring it; callers that DO something should not.

    Re-reads the files rather than caching a flag beside `_CACHE`: this runs
    once an hour from a scheduled job, not on a recall path, and a second
    stat-and-parse is cheaper than a second piece of cache state that can go
    stale independently of the one it shadows.
    """
    base_path = _base_path()
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
    except FileNotFoundError:
        # ABSENT is not CORRUPT. A fresh install has no file and the defaults
        # are the intended answer; refusing here would make "no config" mean
        # "do nothing", which is a different and wrong reading.
        return True
    except Exception:
        return False
    if not isinstance(loaded, dict):
        return False
    try:
        merge_local_overlay(loaded, base_path)
    except Exception:
        return False
    return True


def effective_mode() -> str:
    """The store to use: ``networkx`` or ``falkordb``. See ``selection``."""
    return selection()[0]


def selection() -> tuple[str, str | None]:
    """``(mode, quiet_reason)``: the store to use, and why it is a quiet networkx.

    Four ways to land on networkx, in precedence order: the env kill switch,
    `enabled` being anything other than exactly True, anything the file says
    that is not a recognised mode, and the where-present guard. Only the guard
    sets ``quiet_reason`` (``ENGINE_NOT_PROVISIONED``): falkordb comes from the
    shipped file alone and no enabled engine unit exists here. It is never reported as
    an error, because the traversal telemetry counts an error as a broken clock.
    """
    mode = _configured_mode()
    if mode == "falkordb" and not mode_set_by_overlay() and not engine_unit_present():
        return "networkx", ENGINE_NOT_PROVISIONED
    return mode, None


def _configured_mode() -> str:
    """The lever's mode before the where-present guard."""
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
