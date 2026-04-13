"""Config loader for inbox monitor — YAML → InboxConfig."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from genesis.env import user_timezone
from genesis.inbox.types import InboxConfig


def load_inbox_config(path: str | Path) -> InboxConfig:
    """Load inbox config from a YAML file path."""
    text = Path(path).read_text()
    return load_inbox_config_from_string(text)


def load_inbox_config_from_string(text: str) -> InboxConfig:
    """Load inbox config from a YAML string."""
    raw = yaml.safe_load(text)
    return _parse(raw)


def _parse(raw: dict) -> InboxConfig:
    """Parse raw YAML dict into a validated InboxConfig."""
    if not isinstance(raw, dict):
        msg = "Config must be a YAML mapping"
        raise ValueError(msg)

    section = raw.get("inbox_monitor")
    if section is None:
        msg = "Config must contain 'inbox_monitor' section"
        raise ValueError(msg)

    if "watch_path" not in section:
        msg = "inbox_monitor.watch_path is required"
        raise KeyError(msg)

    return InboxConfig(
        watch_path=Path(
            os.environ.get("GENESIS_INBOX_PATH", section["watch_path"]),
        ).expanduser(),
        response_dir=section.get("response_dir", "_genesis"),
        check_interval_seconds=int(section.get("check_interval_seconds", 1800)),
        batch_size=int(section.get("batch_size", 5)),
        enabled=bool(section.get("enabled", True)),
        model=str(section.get("model", "sonnet")),
        effort=str(section.get("effort", "high")),
        timeout_s=int(section.get("timeout_s", 600)),
        max_retries=int(section.get("max_retries", 3)),
        recursive=bool(section.get("recursive", False)),
        timezone=str(section.get("timezone", user_timezone())),
        evaluation_cooldown_seconds=int(
            section.get("evaluation_cooldown_seconds", 3600),
        ),
    )
