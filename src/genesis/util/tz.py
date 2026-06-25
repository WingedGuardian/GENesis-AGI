"""Timezone display helpers — UTC storage, user-local display.

All Genesis timestamps are stored as UTC ISO 8601 strings. This module
provides display-time conversion to the user's local timezone. Never
use these functions for storage — only for rendering to the user.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from genesis.env import user_timezone as _get_user_timezone

_DEFAULT_TZ = "UTC"

try:
    _USER_TZ = ZoneInfo(_get_user_timezone())
except (ZoneInfoNotFoundError, KeyError):
    _USER_TZ = ZoneInfo(_DEFAULT_TZ)


def reload() -> str:
    """Re-read user timezone and update the module-level cache.

    Call after changing the timezone in genesis.yaml. Returns the new
    timezone name.
    """
    global _USER_TZ
    tz_name = _get_user_timezone()
    try:
        _USER_TZ = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        _USER_TZ = ZoneInfo(_DEFAULT_TZ)
        tz_name = _DEFAULT_TZ
    return tz_name


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


def parse_utc_iso(iso_str: str | None) -> datetime | None:
    """Parse a stored ISO-8601 timestamp into a UTC-aware ``datetime``.

    Genesis stores timestamps as UTC ISO strings. A value written without an
    offset (naive) is treated as UTC. Returns ``None`` on empty or unparseable
    input so callers can branch explicitly instead of silently swallowing the
    error and disabling a guard.

    Unlike :func:`fmt`, this is for *comparison/arithmetic* on the read path,
    not display. Use it wherever a stored timestamp is subtracted from
    ``datetime.now(UTC)`` so a naive value can never raise
    ``can't subtract offset-naive and offset-aware datetimes``.
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt
