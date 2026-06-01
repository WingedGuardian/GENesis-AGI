"""Ego configuration loader — reads/writes config/ego.yaml.

Follows the outreach config pattern: YAML-backed, atomic writes,
dataclass with sensible defaults.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
from pathlib import Path

import yaml

from genesis.ego.types import EgoConfig

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"


def _config_path() -> Path:
    return _CONFIG_DIR / "ego.yaml"


def load_ego_config(path: Path | None = None) -> EgoConfig:
    """Load ego config from YAML. Returns defaults if file missing."""
    if path is None:
        path = _config_path()
    if not path.exists():
        logger.info("Ego config not found at %s — using defaults", path)
        return EgoConfig()

    try:
        from genesis._config_overlay import merge_local_overlay

        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        raw = merge_local_overlay(raw, path)
    except Exception:
        logger.error("Failed to read ego config from %s", path, exc_info=True)
        return EgoConfig()

    # Build config from YAML, using EgoConfig defaults for missing keys.
    kwargs = {}
    for field_name, field_obj in EgoConfig.__dataclass_fields__.items():
        if field_name in raw:
            value = raw[field_name]
            # Guard: YAML null → None for dict fields would crash .get()
            # at runtime. Fall back to the field default instead.
            if value is None and field_obj.default_factory is not dataclasses.MISSING:
                continue
            kwargs[field_name] = value
    return EgoConfig(**kwargs)


def save_ego_config(config: EgoConfig, path: Path | None = None) -> None:
    """Atomic write of ego config to YAML."""
    if path is None:
        path = _config_path()

    data = {
        k: v
        for k, v in config.__dict__.items()
        if k in EgoConfig.__dataclass_fields__
    }

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".yaml.tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            f.write("# Ego session configuration\n\n")
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        Path(tmp_path).replace(path)
        logger.info("Ego config saved to %s", path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def validate_ego_config(changes: dict) -> list[str]:
    """Validate proposed ego config changes. Returns list of error strings."""
    errors = []
    if "cadence_minutes" in changes:
        v = changes["cadence_minutes"]
        if not isinstance(v, (int, float)) or v < 1:
            errors.append("cadence_minutes must be >= 1")
    if "activity_threshold_minutes" in changes:
        v = changes["activity_threshold_minutes"]
        if not isinstance(v, (int, float)) or v < 1:
            errors.append("activity_threshold_minutes must be >= 1")
    if "max_interval_minutes" in changes:
        v = changes["max_interval_minutes"]
        if not isinstance(v, (int, float)) or v < 1:
            errors.append("max_interval_minutes must be >= 1")
    if "backoff_multiplier" in changes:
        v = changes["backoff_multiplier"]
        if not isinstance(v, (int, float)) or v < 1.0:
            errors.append("backoff_multiplier must be >= 1.0")
    if "model" in changes:
        valid_models = {"opus", "sonnet", "haiku"}
        if changes["model"] not in valid_models:
            errors.append(f"model must be one of {valid_models}")
    if "board_size" in changes:
        v = changes["board_size"]
        if not isinstance(v, int) or v < 1:
            errors.append("board_size must be integer >= 1")
    if "consecutive_failure_limit" in changes:
        v = changes["consecutive_failure_limit"]
        if not isinstance(v, int) or v < 1:
            errors.append("consecutive_failure_limit must be integer >= 1")
    if "morning_report_hour" in changes:
        v = changes["morning_report_hour"]
        if not isinstance(v, int) or not (0 <= v <= 23):
            errors.append("morning_report_hour must be 0-23")
    if "morning_report_minute" in changes:
        v = changes["morning_report_minute"]
        if not isinstance(v, int) or not (0 <= v <= 59):
            errors.append("morning_report_minute must be 0-59")
    _VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}
    if "default_effort" in changes and changes["default_effort"] not in _VALID_EFFORTS:
        errors.append(f"default_effort must be one of {_VALID_EFFORTS}")
    if "morning_report_effort" in changes and changes["morning_report_effort"] not in _VALID_EFFORTS:
        errors.append(f"morning_report_effort must be one of {_VALID_EFFORTS}")
    if "genesis_cadence_minutes" in changes:
        v = changes["genesis_cadence_minutes"]
        if not isinstance(v, (int, float)) or v < 30:
            errors.append("genesis_cadence_minutes must be >= 30")
    if "genesis_max_interval_minutes" in changes:
        v = changes["genesis_max_interval_minutes"]
        if not isinstance(v, (int, float)) or v < 60:
            errors.append("genesis_max_interval_minutes must be >= 60")
    if "dispatch_model_overrides" in changes:
        v = changes["dispatch_model_overrides"]
        valid_models = {"opus", "sonnet", "haiku"}
        if not isinstance(v, dict):
            errors.append("dispatch_model_overrides must be a dict")
        else:
            for action, model in v.items():
                if model not in valid_models:
                    errors.append(f"dispatch_model_overrides[{action}]: model must be one of {valid_models}")
    return errors
