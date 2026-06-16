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


def merge_local_overlay(base: dict, base_path: Path) -> dict:
    """Deep-merge a ``.local.yaml`` overlay into *base* if it exists.

    *base_path* is the path to the base YAML file (e.g.
    ``config/inbox_monitor.yaml``).  The overlay is resolved user-dir-first
    (``~/.genesis/config/{stem}.local.yaml``), falling back to the repo-relative
    sibling.

    Returns *base* unchanged when no overlay file exists.
    """
    local_path = _resolve_overlay_path(base_path)
    if not local_path.exists():
        return base
    try:
        local = yaml.safe_load(local_path.read_text()) or {}
    except Exception:
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
