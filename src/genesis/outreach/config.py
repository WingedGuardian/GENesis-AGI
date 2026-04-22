"""Outreach configuration — YAML loader with sensible defaults."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuietHours:
    start: str
    end: str
    timezone: str


@dataclass(frozen=True)
class OutreachConfig:
    quiet_hours: QuietHours
    channel_preferences: dict[str, str]
    thresholds: dict[str, float]
    max_daily: int
    surplus_daily: int
    content_daily: int
    morning_report_time: str
    morning_report_timezone: str
    engagement_timeout_hours: int
    engagement_poll_minutes: int
    immediate_escalation_alerts: tuple[str, ...] = (
        "infra:tmpfs_low",
        "infra:disk_low",
        "infra:container_memory_high",
        "cc:quota_exhausted",
        "provider:embedding_failing",
        "provider:qdrant_unreachable",
        "awareness:tick_overdue",
        "service:health_data_uninitialized",
    )
    # Delivery routing: per-category target — "supergroup", "dm", or "both".
    # Falls back to "default" key, then "supergroup" if unset.
    # When forum_chat_id is not configured, "supergroup" degrades to DM.
    delivery_routing: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.delivery_routing is None:
            object.__setattr__(self, "delivery_routing", {"default": "supergroup"})


def _default_tz() -> str:
    """Resolve default timezone at call time, not import time."""
    try:
        from genesis.env import user_timezone
        return user_timezone()
    except Exception:
        return os.environ.get("USER_TIMEZONE", "UTC")


_DEFAULTS = OutreachConfig(
    quiet_hours=QuietHours(start="22:00", end="07:00", timezone="UTC"),
    channel_preferences={"default": "telegram"},
    thresholds={"blocker": 0.0, "alert": 0.3, "surplus": 0.7, "digest": 0.0},
    max_daily=5,
    surplus_daily=1,
    content_daily=3,
    morning_report_time="07:00",
    morning_report_timezone="UTC",
    engagement_timeout_hours=24,
    engagement_poll_minutes=60,
    immediate_escalation_alerts=(
        "infra:tmpfs_low",
        "infra:disk_low",
        "infra:container_memory_high",
        "cc:quota_exhausted",
        "provider:embedding_failing",
        "provider:qdrant_unreachable",
        "awareness:tick_overdue",
        "service:health_data_uninitialized",
    ),
    delivery_routing={"default": "supergroup"},
)


def _config_path() -> Path:
    """Return the default outreach config path."""
    return Path(__file__).parent.parent.parent.parent / "config" / "outreach.yaml"


def validate_preferences(preferences: dict) -> list[str]:
    """Validate user-supplied preferences dict. Returns list of error strings (empty = valid)."""
    errors: list[str] = []

    if "quiet_hours" in preferences:
        qh = preferences["quiet_hours"]
        if not isinstance(qh, dict):
            errors.append("quiet_hours must be a dict")
        else:
            for field in ("start", "end"):
                val = qh.get(field)
                if val is not None:
                    import re
                    if not re.fullmatch(r"\d{2}:\d{2}", str(val)):
                        errors.append(f"quiet_hours.{field} must be HH:MM format, got {val!r}")
            tz = qh.get("timezone")
            if tz is not None:
                try:
                    from zoneinfo import ZoneInfo
                    ZoneInfo(str(tz))
                except (KeyError, Exception):
                    errors.append(f"quiet_hours.timezone: unknown timezone {tz!r}")

    if "thresholds" in preferences:
        for k, v in preferences["thresholds"].items():
            try:
                fv = float(v)
                if not 0.0 <= fv <= 1.0:
                    errors.append(f"thresholds.{k} must be 0.0–1.0, got {fv}")
            except (TypeError, ValueError):
                errors.append(f"thresholds.{k}: not a number: {v!r}")

    if "rate_limits" in preferences:
        rl = preferences["rate_limits"]
        for field in ("max_daily", "surplus_daily", "content_daily"):
            val = rl.get(field)
            if val is not None:
                try:
                    iv = int(val)
                    if iv < 0:
                        errors.append(f"rate_limits.{field} must be >= 0, got {iv}")
                except (TypeError, ValueError):
                    errors.append(f"rate_limits.{field}: not an integer: {val!r}")

    return errors


def save_outreach_config(config: OutreachConfig, path: Path | None = None) -> None:
    """Serialize OutreachConfig back to YAML. Atomic write via temp file + rename."""
    if path is None:
        path = _config_path()

    data: dict = {
        "quiet_hours": {
            "start": config.quiet_hours.start,
            "end": config.quiet_hours.end,
            "timezone": config.quiet_hours.timezone,
        },
        "channel_preferences": dict(config.channel_preferences),
        "thresholds": dict(config.thresholds),
        "rate_limits": {
            "max_daily": config.max_daily,
            "surplus_daily": config.surplus_daily,
            "content_daily": config.content_daily,
        },
        "morning_report": {
            "trigger_time": config.morning_report_time,
            "timezone": config.morning_report_timezone,
        },
        "engagement": {
            "timeout_hours": config.engagement_timeout_hours,
            "poll_interval_minutes": config.engagement_poll_minutes,
        },
        "health_alerts": {
            "immediate_escalation": list(config.immediate_escalation_alerts),
        },
        "delivery_routing": dict(config.delivery_routing),
    }

    import tempfile
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".yaml.tmp",
    )
    try:
        with open(tmp_fd, "w") as f:
            f.write("# Phase 8: Outreach pipeline configuration\n\n")
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        Path(tmp_path).replace(path)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def load_outreach_config(path: Path | None = None) -> OutreachConfig:
    """Load outreach config from YAML. Returns defaults if file missing."""
    if path is None:
        path = _config_path()
    if not path.exists():
        logger.warning("Outreach config not found at %s — using UTC defaults", path)
        return _DEFAULTS
    from genesis._config_overlay import merge_local_overlay

    with open(path) as f:
        raw = yaml.safe_load(f) or {}
    raw = merge_local_overlay(raw, path)
    qh = raw.get("quiet_hours", {})
    tz = _default_tz()
    return OutreachConfig(
        quiet_hours=QuietHours(
            start=qh.get("start", "22:00"),
            end=qh.get("end", "07:00"),
            timezone=qh.get("timezone") or tz,
        ),
        channel_preferences=raw.get("channel_preferences", {"default": "telegram"}),
        thresholds=raw.get("thresholds", _DEFAULTS.thresholds),
        max_daily=raw.get("rate_limits", {}).get("max_daily", 5),
        surplus_daily=raw.get("rate_limits", {}).get("surplus_daily", 1),
        content_daily=raw.get("rate_limits", {}).get("content_daily", 3),
        morning_report_time=raw.get("morning_report", {}).get("trigger_time", "07:00"),
        morning_report_timezone=raw.get("morning_report", {}).get("timezone") or tz,
        engagement_timeout_hours=raw.get("engagement", {}).get("timeout_hours", 24),
        engagement_poll_minutes=raw.get("engagement", {}).get("poll_interval_minutes", 60),
        immediate_escalation_alerts=tuple(
            raw.get("health_alerts", {}).get(
                "immediate_escalation",
                _DEFAULTS.immediate_escalation_alerts,
            )
        ),
        delivery_routing=raw.get("delivery_routing", {"default": "supergroup"}),
    )
