"""Best-effort parse of a CC rate-limit signal into a resume schedule.

Pure + injected clock. Cascades: structured ``raw_event`` → prose ``raw_text``
→ ``None``. The reset time is a *floor* for the resume scheduler, never a
promise — resume is self-validating (a still-limited re-run re-parks), so an
imprecise or missing reset only changes WHEN the first re-attempt fires, not
whether the parked work survives.

CRITICAL UNKNOWN — verify against the first real captured event: the exact
field layout of a CC ``rate_limit_event`` payload is not documented, and the
prose form ("resets Xpm") is only known from a code comment. This parser
therefore searches defensively for common reset-time keys and falls back to
prose; on any miss it returns ``None`` and the scheduler uses its cadence
floor. A weekly limit with only a wall-clock time is day-ambiguous → ``None``
(never guess "next 5pm").
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

# limit_kind vocabulary — kept tiny; only used to size backoff and to decide
# whether a bare wall-clock prose time is day-ambiguous.
SESSION = "session"
WEEKLY = "weekly"
UNKNOWN = "unknown"

# A parsed absolute reset further than this from ``now`` is treated as
# implausible (bad parse) and dropped to None — the cadence floor is safer than
# a wild timestamp.
_MAX_HORIZON = timedelta(days=14)

# Candidate keys (normalised: lowercased, non-alphanumerics stripped) that may
# carry a reset time in a structured payload. Duration-style keys (retry-after,
# *_in, *_seconds) are interpreted relative to ``now``; the rest as absolute.
_ABSOLUTE_KEYS = frozenset(
    {"resetsat", "resetat", "reset", "resettime", "resettimestamp", "windowresetsat"}
)
_DURATION_KEYS = frozenset(
    {"retryafter", "retryafterseconds", "resetin", "resetinseconds", "secondsuntilreset"}
)


def _norm_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _blob(raw_event: dict | None, raw_text: str | None) -> str:
    """Lowercased text blob for keyword detection (event stringified + prose)."""
    parts: list[str] = []
    if raw_event is not None:
        parts.append(str(raw_event))
    if raw_text:
        parts.append(raw_text)
    return " ".join(parts).lower()


def detect_limit_kind(raw_event: dict | None, raw_text: str | None) -> str:
    """Best-effort {session|weekly|unknown} from the signal's text."""
    blob = _blob(raw_event, raw_text)
    if not blob:
        return UNKNOWN
    if "week" in blob:
        return WEEKLY
    if "session" in blob or "5-hour" in blob or "5 hour" in blob or "five hour" in blob:
        return SESSION
    return UNKNOWN


def _coerce_number(value: float, now: datetime, *, is_duration: bool) -> datetime | None:
    """Turn a numeric reset value into an absolute UTC datetime, or None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if is_duration:
        return now + timedelta(seconds=v)
    # Absolute epoch: seconds (~1e9–1e11) or milliseconds (>=1e12).
    if v >= 1e12:
        v /= 1000.0
    if v >= 1e9:
        try:
            return datetime.fromtimestamp(v, tz=now.tzinfo)
        except (OSError, OverflowError, ValueError):
            return None
    # Small bare number with an absolute key is ambiguous — treat as seconds
    # from now (retry-after leakage) rather than an epoch near 1970.
    return now + timedelta(seconds=v)


def _parse_iso(text: str, now: datetime) -> datetime | None:
    s = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=now.tzinfo)
    return dt


def _reset_from_event(
    raw_event: dict | None, now: datetime, limit_kind: str = UNKNOWN
) -> datetime | None:
    """Recursively hunt for a reset-time key in a structured payload.

    ``limit_kind`` is threaded so a wall-clock string value under an absolute
    key honors the same weekly day-ambiguity guard as the prose path (a weekly
    "5pm" is unknowable → None, never a guessed concrete time)."""
    if not isinstance(raw_event, dict):
        return None
    for key, value in raw_event.items():
        nk = _norm_key(str(key))
        if nk in _DURATION_KEYS and isinstance(value, (int, float)) and not isinstance(value, bool):
            dt = _coerce_number(value, now, is_duration=True)
            if dt is not None:
                return dt
        if nk in _ABSOLUTE_KEYS:
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)):
                dt = _coerce_number(value, now, is_duration=False)
                if dt is not None:
                    return dt
            if isinstance(value, str):
                dt = _parse_iso(value, now) or _reset_from_prose(value, now, limit_kind)
                if dt is not None:
                    return dt
    # Recurse into nested dicts/lists (payload shape unknown).
    for value in raw_event.values():
        if isinstance(value, dict):
            dt = _reset_from_event(value, now, limit_kind)
            if dt is not None:
                return dt
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    dt = _reset_from_event(item, now, limit_kind)
                    if dt is not None:
                        return dt
    return None


_DURATION_RE = re.compile(
    r"reset[a-z ]*?in[:\s]*"
    r"(?:(?P<h>\d+)\s*(?:h|hour|hours|hr))?\s*"
    r"(?:(?P<m>\d+)\s*(?:m|min|minute|minutes))?",
    re.IGNORECASE,
)
_CLOCK_RE = re.compile(
    r"reset[a-z ]*?(?:at\s*)?(?P<h>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ap>am|pm)",
    re.IGNORECASE,
)


def _reset_from_prose(text: str, now: datetime, limit_kind: str) -> datetime | None:
    """Parse a reset time from CC's prose. Relative durations are unambiguous;
    a bare wall-clock time is day-ambiguous for a WEEKLY limit → None."""
    if not text:
        return None
    low = text.lower()

    # Relative duration: "resets in 2h 30m" / "Resets in: 45 minutes".
    m = _DURATION_RE.search(low)
    if m and (m.group("h") or m.group("m")):
        hours = int(m.group("h") or 0)
        mins = int(m.group("m") or 0)
        if hours or mins:
            return now + timedelta(hours=hours, minutes=mins)

    # Wall-clock: "resets 5pm" / "resets at 11:30 am".
    c = _CLOCK_RE.search(low)
    if c:
        if limit_kind == WEEKLY:
            # Which day? Unknowable from a bare clock time — don't guess.
            return None
        hour = int(c.group("h")) % 12
        if c.group("ap").lower() == "pm":
            hour += 12
        minute = int(c.group("min") or 0)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    return None


def parse_reset(
    *,
    raw_event: dict | None = None,
    raw_text: str | None = None,
    now: datetime,
) -> tuple[str, datetime | None]:
    """Return ``(limit_kind, reset_at)``.

    ``limit_kind`` ∈ {session, weekly, unknown}. ``reset_at`` is a tz-aware
    datetime (in ``now``'s tz) or ``None`` when unknown/ambiguous. Never raises
    — a totally unparseable signal yields ``(unknown, None)`` and the scheduler
    falls back to its cadence floor.
    """
    limit_kind = detect_limit_kind(raw_event, raw_text)
    reset_at = _reset_from_event(raw_event, now, limit_kind)
    if reset_at is None and raw_text:
        reset_at = _reset_from_prose(raw_text, now, limit_kind)
    # Sanity clamp: an absurdly-distant parse is an artifact — drop it so the
    # scheduler uses its cadence floor. A past/near reset is fine (the scheduler
    # treats <= now as "due now").
    if reset_at is not None and (reset_at - now) > _MAX_HORIZON:
        reset_at = None
    return limit_kind, reset_at
