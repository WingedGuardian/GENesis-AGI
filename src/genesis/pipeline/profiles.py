"""Research profile loader — reads YAML configs for pipeline profiles."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from genesis.pipeline.types import Tier

logger = logging.getLogger(__name__)

# Local overlay: user-specific profiles live here, not in the repo.
_LOCAL_PROFILES_DIR = Path.home() / ".genesis" / "config" / "research-profiles"


@dataclass
class SourceConfig:
    name: str
    type: str  # "web_search", "api", "rss", "ai_search"
    queries: list[str] = field(default_factory=list)
    endpoint: str | None = None
    refresh_interval_hours: float = 4.0
    params: dict = field(default_factory=dict)


@dataclass
class ResearchProfile:
    name: str
    enabled: bool = True
    tier0_interval_minutes: int = 30
    tier1_batch_size: int = 50
    tier2_trigger_threshold: int = 10
    sources: list[SourceConfig] = field(default_factory=list)
    relevance_keywords: list[str] = field(default_factory=list)
    exclude_keywords: list[str] = field(default_factory=list)
    min_relevance: float = 0.3
    notify_on_tier: Tier = Tier.JUDGMENT
    store_config: dict = field(default_factory=dict)


class ProfileLoader:
    def __init__(self, config_dir: Path | str = "config/research-profiles"):
        self._config_dir = Path(config_dir)
        self._profiles: dict[str, ResearchProfile] = {}

    def load_all(self) -> dict[str, ResearchProfile]:
        """Load all YAML profiles from the config directory."""
        self._profiles.clear()
        if not self._config_dir.exists():
            logger.warning("Profile config directory does not exist: %s", self._config_dir)
            return self._profiles
        for path in sorted(self._config_dir.glob("*.yaml")):
            try:
                profile = self.load_profile(path)
                self._profiles[profile.name] = profile
            except Exception:
                logger.warning("Failed to load profile from %s", path, exc_info=True)
        for path in sorted(self._config_dir.glob("*.yml")):
            if path.with_suffix(".yaml").exists():
                continue  # skip if .yaml variant already loaded
            try:
                profile = self.load_profile(path)
                self._profiles[profile.name] = profile
            except Exception:
                logger.warning("Failed to load profile from %s", path, exc_info=True)
        logger.info("Loaded %d research profiles", len(self._profiles))
        return self._profiles

    def load_profile(self, path: Path) -> ResearchProfile:
        """Load a single profile from YAML."""
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}

        sources = []
        for src in data.get("sources", []):
            sources.append(
                SourceConfig(
                    name=src.get("name", "unnamed"),
                    type=src.get("type", "web_search"),
                    queries=src.get("queries", []),
                    endpoint=src.get("endpoint"),
                    refresh_interval_hours=src.get("refresh_interval_hours", 4.0),
                    params=src.get("params", {}),
                )
            )

        notify_tier_raw = data.get("notify_on_tier", Tier.JUDGMENT)
        if isinstance(notify_tier_raw, str):
            notify_tier = Tier[notify_tier_raw.upper()]
        elif isinstance(notify_tier_raw, int):
            notify_tier = Tier(notify_tier_raw)
        else:
            notify_tier = Tier.JUDGMENT

        return ResearchProfile(
            name=data.get("name", path.stem),
            enabled=data.get("enabled", True),
            tier0_interval_minutes=data.get("tier0_interval_minutes", 30),
            tier1_batch_size=data.get("tier1_batch_size", 50),
            tier2_trigger_threshold=data.get("tier2_trigger_threshold", 10),
            sources=sources,
            relevance_keywords=data.get("relevance_keywords", []),
            exclude_keywords=data.get("exclude_keywords", []),
            min_relevance=data.get("min_relevance", 0.3),
            notify_on_tier=notify_tier,
            store_config=data.get("store_config", {}),
        )

    def merge_overlay(
        self,
        overlay_dir: Path | str | None = None,
    ) -> dict[str, ResearchProfile]:
        """Merge profiles from an overlay directory into the loaded set.

        Local profiles take precedence over repo profiles on name collision.
        overlay_dir defaults to ~/.genesis/config/research-profiles/.
        Call after load_all() to include user-specific profiles not in the repo.
        """
        path = Path(overlay_dir) if overlay_dir is not None else _LOCAL_PROFILES_DIR
        if not path.is_dir():
            return self._profiles
        for yaml_path in sorted(path.glob("*.yaml")):
            try:
                profile = self.load_profile(yaml_path)
                if profile.name in self._profiles:
                    logger.debug("Local profile overrides repo: %s", profile.name)
                self._profiles[profile.name] = profile
            except Exception:
                logger.warning("Failed to load overlay profile from %s", yaml_path, exc_info=True)
        for yml_path in sorted(path.glob("*.yml")):
            if yml_path.with_suffix(".yaml").exists():
                continue  # skip if .yaml variant already loaded
            try:
                profile = self.load_profile(yml_path)
                if profile.name in self._profiles:
                    logger.debug("Local profile overrides repo: %s", profile.name)
                self._profiles[profile.name] = profile
            except Exception:
                logger.warning("Failed to load overlay profile from %s", yml_path, exc_info=True)
        logger.debug("After overlay: %d profiles total", len(self._profiles))
        return self._profiles

    def get(self, name: str) -> ResearchProfile | None:
        return self._profiles.get(name)

    def list_enabled(self) -> list[ResearchProfile]:
        return [p for p in self._profiles.values() if p.enabled]
