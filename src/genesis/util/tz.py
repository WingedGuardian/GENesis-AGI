"""Timezone helpers — UTC storage, user-local display and comparison.

All Genesis timestamps are stored as UTC ISO 8601 strings. This module has two
kinds of helper; NEITHER is for storage (always store UTC):

- Display helpers (``fmt``, ``fmt_short``) — convert a stored UTC string to a
  user-local formatted string for rendering. Never use their output for storage
  or comparison.
- Read-path comparison helpers (``parse_utc_iso``, ``local_day_boundary``) —
  return UTC-aware ``datetime``s for arithmetic/comparison on the read path
  (e.g. "has a new local day started?"), never for display.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from genesis.env import user_timezone as _get_user_timezone

_DEFAULT_TZ = "UTC"

# Bounds the candidate window in local_day_boundary. Candidates span one local
# date AHEAD of now (needed for a fall-back that regresses the local date) back
# through this many days; a real boundary at or before now always exists within
# that span because no DST transition shifts the local date by more than one day
# in either direction.
_MAX_BOUNDARY_LOOKBACK_DAYS = 3

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


def local_day_boundary(hour: int = 0, *, now: datetime | None = None) -> datetime:
    """Most recent day boundary at *hour* local time, as a UTC-aware datetime.

    The boundary is ``hour:00`` in the user's local timezone: the most recent
    such instant at or before *now* (compared in UTC), so the result is never in
    the future. This is a read-path comparison helper (like
    :func:`parse_utc_iso`), not a display helper: it lets "has a new local day
    started since <stored UTC timestamp>?" be answered correctly regardless of
    the gap between the user's timezone and UTC.

    Returned in **UTC** on purpose. Genesis stores timestamps as UTC ISO strings
    (``...+00:00``); those sort/compare lexicographically only when every value
    carries the same offset, so a boundary serialized with a local offset
    (``...-04:00``) would silently break a string ``<`` comparison in SQL. UTC
    return keeps ``.isoformat()`` and ``datetime`` comparisons both correct.

    DST correctness. A naive "today's ``hour:00``, else yesterday's" is wrong
    because the ``hour:00`` wall time can be nonexistent (spring-forward gap),
    ambiguous (fall-back repeats it), or land on a local date a dateline change
    deleted or shifted. Two shapes in particular defeat single-direction logic:

    * a whole calendar day can be skipped, so the most recent real boundary is
      two local dates back (Pacific/Apia dropped 2011-12-30);
    * a >1h fall-back can move the wall clock backward across the boundary hour,
      *regressing* the local date, so the most recent boundary sits on a local
      date one day AHEAD of *now*'s local date yet already elapsed in UTC
      (Antarctica/Casey, hour=0, on 2010-03-04).

    So candidates are generated over a window spanning one local date ahead of
    *now* back through :data:`_MAX_BOUNDARY_LOOKBACK_DAYS`, each normalized with
    ``fold=0`` (an ambiguous boundary pins to its FIRST occurrence — a daily
    reset must fire once, not twice), and the most recent candidate whose UTC
    instant is ``<= now`` is returned. This is monotonic in *now* and never in
    the future. The window is sufficient because no real DST transition shifts
    the local date by more than one day in either direction.

    *now* (UTC-aware) is injectable for deterministic tests; defaults to the
    current instant.
    """
    if now is None:
        now_utc = datetime.now(UTC)
    elif now.tzinfo is None:
        # Naive input is treated as UTC (matches parse_utc_iso's convention),
        # never as system-local — the module stores/compares everything in UTC.
        now_utc = now.replace(tzinfo=UTC)
    else:
        now_utc = now.astimezone(UTC)
    now_local = now_utc.astimezone(_USER_TZ)
    # Generate fold=0 ``hour:00`` candidates from one local date ahead of now
    # back through the lookback window, and return the most recent at or before
    # now (in UTC). Taking the max <= now — rather than the first while walking
    # backward — is what stays correct when local-date order does not track UTC
    # order (the whole-day-skip and fall-back-across-midnight cases above).
    candidates = [
        (now_local - timedelta(days=days_back))
        .replace(hour=hour, minute=0, second=0, microsecond=0, fold=0)
        .astimezone(UTC)
        for days_back in range(-1, _MAX_BOUNDARY_LOOKBACK_DAYS)
    ]
    past = [c for c in candidates if c <= now_utc]
    # Fail-safe (should be unreachable — a real boundary always exists within
    # ~1 day): the oldest candidate, never a future instant.
    return max(past) if past else min(candidates)
