"""Shared helper for merging .local.yaml config overlays.

The dashboard settings system writes user customizations to
``config/{name}.local.yaml``. Each subsystem config loader calls
:func:`merge_local_overlay` after reading the base YAML to pick up
those overrides.

This module is intentionally dependency-free (only stdlib + yaml)
to avoid circular imports from any config loader.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def merge_local_overlay(base: dict, base_path: Path) -> dict:
    """Deep-merge a ``.local.yaml`` overlay into *base* if it exists.

    *base_path* is the path to the base YAML file (e.g.
    ``config/inbox_monitor.yaml``).  The overlay path is derived by
    inserting ``.local`` before the ``.yaml`` suffix.

    Returns *base* unchanged when no overlay file exists.
    """
    local_path = base_path.with_suffix(".local.yaml")
    if not local_path.exists():
        return base
    try:
        local = yaml.safe_load(local_path.read_text()) or {}
    except Exception:
        return base
    return _deep_merge(base, local)


def local_overlay_mtime(base_path: Path) -> float:
    """Return the mtime of the ``.local.yaml`` overlay, or ``0.0``."""
    local_path = base_path.with_suffix(".local.yaml")
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
