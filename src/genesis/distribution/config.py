"""Distribution configuration — loaded from ~/.genesis/config/distribution.yaml."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = Path.home() / ".genesis" / "config" / "distribution.yaml"


@dataclass(frozen=True)
class LinkedInConfig:
    """LinkedIn-specific distribution config."""

    connected_account_id: str = ""
    author_urn: str = ""
    user_id: str = "genesis"
    daily_post_limit: int = 80


@dataclass(frozen=True)
class DistributionConfig:
    """Top-level distribution configuration."""

    linkedin: LinkedInConfig = field(default_factory=LinkedInConfig)


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

        linkedin_raw = raw.get("linkedin", {})
        linkedin = LinkedInConfig(
            connected_account_id=str(linkedin_raw.get("connected_account_id", "")),
            author_urn=str(linkedin_raw.get("author_urn", "")),
            user_id=str(linkedin_raw.get("user_id", "genesis")),
            daily_post_limit=int(linkedin_raw.get("daily_post_limit", 80)),
        )
        return DistributionConfig(linkedin=linkedin)
    except Exception:
        logger.warning("Failed to load distribution config from %s", path, exc_info=True)
        return DistributionConfig()
