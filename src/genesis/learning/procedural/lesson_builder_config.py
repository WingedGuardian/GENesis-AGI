"""Whole-session lesson builder lever: ``mode: off | shadow | live``, read live.

Re-read from the merged YAML (``config/lesson_builder.yaml`` + the user overlay
``~/.genesis/config/lesson_builder.local.yaml``) on every extraction cycle, so a
flip takes effect without a restart. An invalid value degrades to ``shadow``,
which stores nothing (the same fail-safe as ``genesis.ledger.ws2_ledger_config``).

In ``shadow`` the builder runs its real model call and its scoping and novelty
checks, then appends each lesson it would have stored to
``~/.genesis/output/lesson_builder_shadow.jsonl`` (:func:`record_shadow_lesson`)
instead of storing it.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports MODES from here, never the reverse.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import output_dir, repo_root

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "live")

_CONFIG_NAME = "lesson_builder.yaml"

SHADOW_FILE = "lesson_builder_shadow.jsonl"
# The shadow record is bounded: past this size it is moved to ``<name>.1``
# (replacing any earlier one) and a fresh file starts.
SHADOW_MAX_BYTES = 5 * 1024 * 1024

DEFAULTS: dict[str, Any] = {"mode": "live"}


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the merged config fresh, per call (defaults, base yaml, overlay).

    The shipped base is ``live``, so a read failure must not fall back to it: an
    unreadable base or a malformed overlay (an owner pausing the builder with a
    typo) reads as ``shadow``, which stores nothing.
    """
    base_path = _base_path()
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"top level is {type(loaded).__name__}, not a mapping")
        merged = {**DEFAULTS, **merge_local_overlay(loaded, base_path, strict=True)}
    except Exception:
        logger.warning(
            "lesson_builder config or its overlay unreadable (base %s) — reading as shadow",
            base_path, exc_info=True,
        )
        return {"mode": "shadow"}
    return merged


def builder_mode() -> str:
    """The lesson builder mode. An invalid value degrades to ``shadow``."""
    mode = load_config().get("mode")
    if mode is False:
        # An unquoted `mode: off` parses as YAML-1.1 boolean False.
        return "off"
    if mode not in MODES:
        logger.warning("lesson_builder has invalid mode %r — degrading to shadow", mode)
        return "shadow"
    return mode


def record_shadow_lesson(entry: dict[str, Any]) -> None:
    """Append one would-be lesson to the shadow record. Never raises."""
    path = output_dir() / SHADOW_FILE
    line = json.dumps({"ts": datetime.now(UTC).isoformat(), **entry}, ensure_ascii=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > SHADOW_MAX_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        logger.warning("lesson_builder shadow record write failed at %s", path, exc_info=True)


def shadow_count(session_id: str, since: datetime) -> int:
    """Shadow-recorded lessons for ``session_id`` at or after ``since``.

    Reads the current record and the rotated ``.1``. Unreadable files and
    malformed lines count as nothing.
    """
    path = output_dir() / SHADOW_FILE
    count = 0
    for candidate in (path.with_name(path.name + ".1"), path):
        try:
            lines = candidate.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
                if entry.get("session_id") == session_id and (
                    datetime.fromisoformat(entry["ts"]) >= since
                ):
                    count += 1
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
    return count
