"""Timezone display helpers — UTC storage, user-local display.

All Genesis timestamps are stored as UTC ISO 8601 strings. This module
provides display-time conversion to the user's local timezone. Never
use these functions for storage — only for rendering to the user.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_TZ = "America/New_York"

try:
    _USER_TZ = ZoneInfo(os.environ.get("USER_TIMEZONE", _DEFAULT_TZ))
except (ZoneInfoNotFoundError, KeyError):
    _USER_TZ = ZoneInfo(_DEFAULT_TZ)


def fmt(iso_str: str, fmt_str: str = "%a %Y-%m-%d %H:%M %Z") -> str:
    """Convert a UTC ISO string to user-local formatted string.

    Falls back to the original string on parse errors, or "unknown" for None/empty.
    """
    if not iso_str:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(_USER_TZ).strftime(fmt_str)
    except (ValueError, TypeError):
        return iso_str


def fmt_short(iso_str: str) -> str:
    """Short time-only format: '14:30 EST'."""
    return fmt(iso_str, "%H:%M %Z")
