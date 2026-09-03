"""Shared helper for merging .local.yaml config overlays.

The dashboard settings system writes user customizations to
``config/{name}.local.yaml``. Each subsystem config loader calls
:func:`merge_local_overlay` after reading the base YAML to pick up
those overrides.

This module is intentionally dependency-free (only stdlib + yaml)
to avoid circular imports from any config loader.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


def _user_config_dir() -> Path:
    """Canonical user config dir (``~/.genesis/config``).

    Defined here rather than imported from ``genesis.env`` so this module stays
    dependency-free (stdlib + yaml only, no import cycles), and as a function so
    tests can monkeypatch it.
    """
    return Path.home() / ".genesis" / "config"


def _resolve_overlay_path(base_path: Path) -> Path:
    """Resolve the ``.local.yaml`` overlay for *base_path*, preferring the user
    config dir (``~/.genesis/config/``) where the dashboard/MCP settings writers
    land, then falling back to the repo-relative sibling for back-compat.

    Mirrors ``settings.py._load_yaml_local`` so loaders and writers agree on the
    overlay location (cfg-001).
    """
    local_name = base_path.with_suffix(".local.yaml").name
    user_path = _user_config_dir() / local_name
    if user_path.is_file():
        return user_path
    return base_path.with_suffix(".local.yaml")


#: Last-warned mtime per overlay path. A broken overlay MUST warn — but several
#: loaders re-read on every call (the immunity gate reloads per check), so an
#: undeduped warning turns one YAML typo into a multiline traceback on every
#: memory or approval operation, flooding the journal until an operator notices.
#: Warn once per bad file VERSION: any mtime CHANGE warns again, so a failed
#: repair stays visible — including a backwards move, which an mtime-preserving
#: restore produces. Keyed by path (not path+mtime) so the map is bounded by the
#: number of overlay files rather than growing once per edit forever.
_WARNED_OVERLAYS: dict[str, float] = {}


def _warn_overlay_once(local_path: Path, msg: str, *args, exc_info: bool = False) -> None:
    """Log an overlay problem once per (path, mtime). Never raises."""
    path_key = str(local_path)
    try:
        mtime = local_path.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _WARNED_OVERLAYS.get(path_key) == mtime:
        return
    _WARNED_OVERLAYS[path_key] = mtime
    logger.warning(msg, *args, exc_info=exc_info)


def merge_local_overlay(base: dict, base_path: Path) -> dict:
    """Deep-merge a ``.local.yaml`` overlay into *base* if it exists.

    *base_path* is the path to the base YAML file (e.g.
    ``config/inbox_monitor.yaml``).  The overlay is resolved user-dir-first
    (``~/.genesis/config/{stem}.local.yaml``), falling back to the repo-relative
    sibling.

    Returns *base* unchanged when no overlay file exists, is unparseable, or is
    valid YAML of the wrong SHAPE.
    """
    local_path = _resolve_overlay_path(base_path)
    if not local_path.exists():
        return base
    try:
        local = yaml.safe_load(local_path.read_text())
        if local is None:
            # Empty file or an explicit `null` — legitimately nothing to merge,
            # and NOT a malformed overlay. Distinguished from the falsy
            # non-mappings below, which an `or {}` default used to swallow
            # without a word: `[]`, `false`, `0` and `""` all became "no
            # overrides" silently, and an empty-list root is precisely the shape
            # this guard advertises catching.
            return base
        if not isinstance(local, dict):
            # Valid YAML, wrong ROOT SHAPE — a list or a bare scalar. This is the
            # one malformed case the except below does NOT catch: parsing
            # succeeds, and the AttributeError from `.items()` would be raised by
            # _deep_merge OUTSIDE this function, propagating to every caller of
            # the config loader instead of degrading to base. Validate here so a
            # wrong-shape overlay fails the same safe way an unparseable one does.
            _warn_overlay_once(
                local_path,
                "Config overlay %s has a %s at its root, not a mapping — "
                "IGNORING it; every setting in this file is NOT in effect. "
                "Fix the YAML and reload.",
                local_path,
                type(local).__name__,
            )
            return base
    except Exception:
        # NEVER silent. Returning `base` is the right FALLBACK, but an unlogged
        # one is indistinguishable from a clean load — the caller sees a valid
        # config and cannot tell that every override in this file was dropped.
        # That is load-bearing for configs whose overlay is the SOLE home of a
        # setting (cc_roster peers, for one): a single YAML typo silently
        # yields "no peers configured", and the discovery moment is the
        # subscription cap. The base-file loader in genesis.cc.roster already
        # warns on the same failure; this is the matching half.
        _warn_overlay_once(
            local_path,
            "Failed to parse config overlay %s — IGNORING it; every setting in "
            "this file is NOT in effect. Fix the YAML and reload.",
            local_path,
            exc_info=True,
        )
        return base
    return _deep_merge(base, local)


def local_overlay_mtime(base_path: Path) -> float:
    """Return the mtime of the ``.local.yaml`` overlay (user-dir-first), or ``0.0``."""
    local_path = _resolve_overlay_path(base_path)
    try:
        return local_path.stat().st_mtime
    except OSError:
        return 0.0


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Recursively merge *overlay* into *base*.  Lists are replaced."""
    merged = dict(base)
    for key, val in overlay.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = _deep_merge(merged[key], val)
        else:
            merged[key] = val
    return merged
