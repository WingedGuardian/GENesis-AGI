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


def load_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    """Load profile.json; missing/corrupt → empty dict."""
    return _load_json(path, "profile")


def save_profile(profile: dict[str, Any], path: Path = PROFILE_PATH) -> None:
    atomic_write_text(path, json.dumps(profile, indent=2, default=str))


def load_annotations(path: Path = ANNOTATIONS_PATH) -> dict[str, Any]:
    """Load annotations.json; missing/corrupt → empty dict."""
    return _load_json(path, "annotations")


def save_annotations(annotations: dict[str, Any], path: Path = ANNOTATIONS_PATH) -> None:
    atomic_write_text(path, json.dumps(annotations, indent=2, default=str))
