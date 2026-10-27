"""Resolve ``preferred_timing`` values for scheduled outreach.

Single source of truth for turning a caller-supplied delivery time — ISO-8601
or natural language — into an aware UTC datetime. The pending_outreach drain
compares ``deliver_after`` lexicographically against a UTC ISO ``now`` string,
so anything stored MUST be ``dt.astimezone(UTC).isoformat()``: a verbatim NL
string sorts after every ``2...`` ISO timestamp and never fires, and an ISO
timestamp with a non-UTC offset compares incorrectly. Both failure modes are
silent — the row sits undeliverable until the 24h age-out drops it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

# Bare clock time: "9am", "9:30pm", "14:00". dateparser's future-preference
# handles these inconsistently across timezones, so they are resolved
# directly: the next occurrence of that local time.
_BARE_CLOCK_RE = re.compile(r"^(\d{1,2})(?::(\d{2}))?\s*([ap]\.?m\.?)?$", re.IGNORECASE)


class TimingParseError(ValueError):
    """Raised when ``preferred_timing`` cannot be resolved to a datetime."""


def resolve_preferred_timing(
    value: str | None,
    *,
    now: datetime | None = None,
    tz: str | None = None,
) -> datetime:
    """Resolve ``value`` to an aware UTC datetime or raise TimingParseError.

    ISO-8601 input is tried first: an aware value resolves to the same instant
    (so existing ISO callers see identical scheduling); a naive value is
    interpreted in the user's timezone. Anything else goes to dateparser's
    natural-language parser, evaluated relative to ``now`` in the user's
    timezone and preferring future dates ("tomorrow 9am", "in 2 hours",
    "next friday 3pm", a bare clock time after the hour has passed → tomorrow).

    ``now``/``tz`` are injectable for tests; when ``tz`` is None the resolution
    timezone comes from ``genesis.env.user_timezone()``.
    """
    import zoneinfo

    raw = (value or "").strip()
    if not raw:
        raise TimingParseError("preferred_timing is empty")

    tzname = tz
    if tzname is None:
        from genesis.env import user_timezone

        tzname = user_timezone()
    try:
        local_tz = zoneinfo.ZoneInfo(tzname)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError, ValueError):
        local_tz = UTC

    base_utc = now or datetime.now(UTC)
    if base_utc.tzinfo is None:
        base_utc = base_utc.replace(tzinfo=UTC)
    base_utc = base_utc.astimezone(UTC)

    # ISO-8601 first — exact-instant semantics for existing callers.
    try:
        dt = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        dt = None
    if dt is not None:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=local_tz)
        return dt.astimezone(UTC)

    # Bare clock time → the next local occurrence (today or tomorrow).
    m = _BARE_CLOCK_RE.match(raw)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or 0)
        meridiem = (m.group(3) or "").lower().replace(".", "")
        if meridiem:
            if not 1 <= hour <= 12:
                raise TimingParseError(
                    f"could not interpret preferred_timing '{raw}'")
            hour = hour % 12 + (12 if meridiem.startswith("p") else 0)
        local_now = base_utc.astimezone(local_tz)
        candidate = local_now.replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(UTC)

    import dateparser

    parsed = dateparser.parse(
        raw,
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": base_utc.astimezone(local_tz),
            "TIMEZONE": tzname,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "PREFER_DAY_OF_MONTH": "first",
        },
    )
    if parsed is None or parsed.tzinfo is None:
        raise TimingParseError(f"could not interpret preferred_timing '{raw}'")
    return parsed.astimezone(UTC)
