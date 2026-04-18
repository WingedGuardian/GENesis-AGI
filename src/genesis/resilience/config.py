"""Load and validate resilience configuration."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent.parent.parent / "config" / "resilience.yaml"


@dataclass
class FlappingConfig:
    transition_count: int = 3
    window_seconds: int = 900
    stabilization_seconds: int = 600


@dataclass
class RecoveryConfig:
    confirmation_probes: int = 3
    confirmation_interval_s: int = 30
    drain_pace_s: int = 30
    embedding_pace_per_min: int = 10
    queue_overflow_threshold: int = 1000


@dataclass
class CCConfig:
    max_sessions_per_hour: int = 20
    throttle_threshold_pct: float = 0.80


@dataclass
class StatusConfig:
    path: str = "~/.genesis/status.json"
    write_every_tick: bool = True


@dataclass
class NotificationConfig:
    webhook_url: str | None = None
    webhook_events: list[str] = field(default_factory=lambda: ["state_changed", "recovery_complete", "queue_overflow"])


@dataclass
class ResilienceConfig:
    flapping: FlappingConfig = field(default_factory=FlappingConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    cc: CCConfig = field(default_factory=CCConfig)
    status: StatusConfig = field(default_factory=StatusConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)


def load_config(path: Path | None = None) -> ResilienceConfig:
    """Load resilience config from YAML. Returns defaults if file missing."""
    config_path = path or _DEFAULT_CONFIG_PATH
    if not config_path.exists():
        logger.info("Resilience config not found at %s, using defaults", config_path)
        return ResilienceConfig()

    from genesis._config_overlay import merge_local_overlay

    with open(config_path) as f:
        raw = yaml.safe_load(f) or {}
    raw = merge_local_overlay(raw, config_path)

    return ResilienceConfig(
        flapping=FlappingConfig(**raw.get("flapping", {})),
        recovery=RecoveryConfig(**raw.get("recovery", {})),
        cc=CCConfig(**raw.get("cc", {})),
        status=StatusConfig(**raw.get("status", {})),
        notifications=NotificationConfig(**raw.get("notifications", {})),
    )
