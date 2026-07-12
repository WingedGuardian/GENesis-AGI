"""Load/save the profile and annotations JSON files.

Writes go through ``atomic_write_text`` (temp + rename) so readers — the MCP
tool, the sentinel digest, out-of-process consumers — never see a torn file.
Corrupt or missing files load as empty: the profile is regenerable state, so
recovery is always "collect again", never "fail".
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from genesis.infra_profile.paths import ANNOTATIONS_PATH, PROFILE_PATH
from genesis.util.atomic import atomic_write_text

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        logger.warning("Cannot read %s (%s): %s", label, path, exc)
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning("Corrupt %s at %s — treating as empty: %s", label, path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Unexpected %s shape at %s — treating as empty", label, path)
        return {}
    return data


# Paths resolve at CALL time (None → module constant), not at def time:
# a default arg binds the constant when the module loads, which silently
# defeats test redirection and any runtime path override (found when a test
# wrote to the real ~/.genesis, 2026-07-12).


def load_profile(path: Path | None = None) -> dict[str, Any]:
    """Load profile.json; missing/corrupt → empty dict."""
    return _load_json(path if path is not None else PROFILE_PATH, "profile")


def save_profile(profile: dict[str, Any], path: Path | None = None) -> None:
    atomic_write_text(
        path if path is not None else PROFILE_PATH,
        json.dumps(profile, indent=2, default=str),
    )


def load_annotations(path: Path | None = None) -> dict[str, Any]:
    """Load annotations.json; missing/corrupt → empty dict."""
    return _load_json(path if path is not None else ANNOTATIONS_PATH, "annotations")


def save_annotations(annotations: dict[str, Any], path: Path | None = None) -> None:
    atomic_write_text(
        path if path is not None else ANNOTATIONS_PATH,
        json.dumps(annotations, indent=2, default=str),
    )
