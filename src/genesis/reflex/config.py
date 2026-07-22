"""Reflex-arc config — loader for ``config/reflex.yaml``.

PR1 surface: ``ingest_enabled`` gates the whole afferent nerve (the
default-event-bus wiring for tracked_task failures AND signal-store
ingestion). Ships OFF; turning it ON requires a server restart (the
subscriber registers at init), turning it OFF is honored live within the
ingestor's refresh interval. ``GENESIS_REFLEX_INGEST_OFF=1`` is the env
kill switch — it wins over any config value (fail toward less activity).

Later phases (cards, dispatch) add their keys here; model/effort choices
stay config, never constants.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "reflex.yaml"


@dataclass(frozen=True)
class ReflexConfig:
    ingest_enabled: bool = False
    source: str = "defaults"


def _env_killed() -> bool:
    return os.environ.get("GENESIS_REFLEX_INGEST_OFF", "").strip() in ("1", "true", "yes")


def load_reflex_config(path: Path | None = None) -> ReflexConfig:
    """Load reflex config; missing/malformed file → safe defaults (OFF)."""
    if _env_killed():
        return ReflexConfig(ingest_enabled=False, source="env_kill")

    cfg_path = path or _CONFIG_PATH
    if not cfg_path.exists():
        return ReflexConfig()

    try:
        from genesis._config_overlay import merge_local_overlay

        raw = yaml.safe_load(cfg_path.read_text()) or {}
        raw = merge_local_overlay(raw, cfg_path)
    except Exception:
        logger.warning(
            "Failed to load reflex config from %s; using defaults", cfg_path, exc_info=True
        )
        return ReflexConfig()

    if not isinstance(raw, dict):
        logger.warning("Reflex config at %s is not a mapping; using defaults", cfg_path)
        return ReflexConfig()

    return ReflexConfig(
        ingest_enabled=bool(raw.get("ingest_enabled", False)),
        source=str(cfg_path),
    )
