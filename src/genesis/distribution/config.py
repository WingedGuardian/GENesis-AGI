"""Distribution configuration — loaded from ~/.genesis/config/distribution.yaml."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path.home() / ".genesis" / "config" / "distribution.yaml"


@dataclass(frozen=True)
class MediumConfig:
    """Medium-specific distribution config."""

    username: str = ""


@dataclass(frozen=True)
class DistributionConfig:
    """Top-level distribution configuration."""

    medium: MediumConfig = field(default_factory=MediumConfig)


def load_distribution_config(
    path: Path | None = None,
) -> DistributionConfig:
    """Load distribution config from YAML, with graceful fallback to defaults."""
    path = path or _DEFAULT_CONFIG_PATH
    if not path.exists():
        logger.info("No distribution config at %s — using defaults", path)
        return DistributionConfig()

    try:
        import yaml

        with path.open() as f:
            raw = yaml.safe_load(f) or {}

        medium_raw = raw.get("medium", {})
        medium = MediumConfig(
            username=str(medium_raw.get("username", "")),
        )
        return DistributionConfig(medium=medium)
    except Exception:
        logger.warning("Failed to load distribution config from %s", path, exc_info=True)
        return DistributionConfig()
