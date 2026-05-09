"""Minimal temporal reference parser for query-time date filtering.

Converts natural language time references in recall queries to ISO date
range tuples. The LLM resolves complex dates at extraction time — this
parser only handles common relative patterns at query time.

Uses stdlib only (no python-dateutil dependency).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_RELATIVE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\byesterday\b", re.I), "yesterday"),
    (re.compile(r"\btoday\b", re.I), "today"),
    (re.compile(r"\blast\s+week\b", re.I), "last_week"),
    (re.compile(r"\bthis\s+week\b", re.I), "this_week"),
    (re.compile(r"\blast\s+month\b", re.I), "last_month"),
    (re.compile(r"\bthis\s+month\b", re.I), "this_month"),
    (re.compile(r"\b(\d+)\s+days?\s+ago\b", re.I), "n_days_ago"),
    (re.compile(r"\b(\d+)\s+weeks?\s+ago\b", re.I), "n_weeks_ago"),
    (re.compile(r"\b(\d+)\s+months?\s+ago\b", re.I), "n_months_ago"),
]

# Temporal marker words for quick pre-check in retrieval
TEMPORAL_MARKERS = re.compile(
    r"\b(when|yesterday|today|last\s+week|last\s+month|this\s+week|"
    r"this\s+month|days?\s+ago|weeks?\s+ago|months?\s+ago|timeline|"
    r"history\s+of|last\s+time|first\s+time)\b",
    re.I,
)


def has_temporal_markers(query: str) -> bool:
    """Quick check if a query contains temporal language."""
    return bool(TEMPORAL_MARKERS.search(query))


def parse_temporal_reference(
    query: str,
    now: datetime | None = None,
) -> tuple[str, str] | None:
    """Parse a relative temporal reference into an ISO date range.

    Returns (start_date, end_date) as ISO date strings (YYYY-MM-DD),
    or None if no temporal pattern is recognized.

    Only the first matching pattern is used.
    """
    if now is None:
        now = datetime.now(UTC)

    today = now.date()

    for pattern, kind in _RELATIVE_PATTERNS:
        m = pattern.search(query)
        if not m:
            continue

        if kind == "yesterday":
            d = today - timedelta(days=1)
            return (d.isoformat(), d.isoformat())

        if kind == "today":
            return (today.isoformat(), today.isoformat())

        if kind == "last_week":
            # Monday-to-Sunday of the previous week
            days_since_monday = today.weekday()
            last_monday = today - timedelta(days=days_since_monday + 7)
            last_sunday = last_monday + timedelta(days=6)
            return (last_monday.isoformat(), last_sunday.isoformat())

        if kind == "this_week":
            monday = today - timedelta(days=today.weekday())
            return (monday.isoformat(), today.isoformat())

        if kind == "last_month":
            first_of_this_month = today.replace(day=1)
            last_day_prev = first_of_this_month - timedelta(days=1)
            first_of_prev = last_day_prev.replace(day=1)
            return (first_of_prev.isoformat(), last_day_prev.isoformat())

        if kind == "this_month":
            first_of_month = today.replace(day=1)
            return (first_of_month.isoformat(), today.isoformat())

        if kind == "n_days_ago":
            n = int(m.group(1))
            d = today - timedelta(days=n)
            return (d.isoformat(), d.isoformat())

        if kind == "n_weeks_ago":
            n = int(m.group(1))
            start = today - timedelta(weeks=n, days=today.weekday())
            end = start + timedelta(days=6)
            return (start.isoformat(), end.isoformat())

        if kind == "n_months_ago":
            n = int(m.group(1))
            year = today.year
            month = today.month - n
            while month <= 0:
                month += 12
                year -= 1
            first = today.replace(year=year, month=month, day=1)
            # Last day of that month
            if month == 12:
                last = today.replace(year=year + 1, month=1, day=1) - timedelta(days=1)
            else:
                last = today.replace(year=year, month=month + 1, day=1) - timedelta(days=1)
            return (first.isoformat(), last.isoformat())

    return None
