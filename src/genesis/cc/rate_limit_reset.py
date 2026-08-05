"""Best-effort parse of a CC rate-limit signal into a resume schedule.

Pure + injected clock. Cascades: structured ``raw_event`` → prose ``raw_text``
→ ``None``. The reset time is a *floor* for the resume scheduler, never a
promise — resume is self-validating (a still-limited re-run re-parks), so an
imprecise or missing reset only changes WHEN the first re-attempt fires, not
whether the parked work survives.

Prose form CONFIRMED against a real captured event (2026-08, reflex signal
CCProcessError×cc): a wall-clock reset time in the ACCOUNT's local zone, named
in a trailing ``(IANA/Zone)`` hint — e.g. ``"You've hit your session limit ·
resets 4:10am (America/Los_Angeles)"``. The parser resolves that hint (falling
back to ``now``'s tz when absent) so a UTC-clocked server does not schedule the
resume hours off. The structured ``rate_limit_event`` payload layout is still
undocumented, so key search stays defensive; on any miss it returns ``None`` and
the scheduler uses its cadence floor. A weekly limit that NAMES a weekday
("resets Monday 9am") resolves to that day's next occurrence; a weekly limit
with only a BARE wall-clock is day-ambiguous → ``None`` (never guess "next 5pm").
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

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
# Weekday-bearing wall-clock, e.g. "resets Monday 9am". A named day makes a
# WEEKLY reset UNambiguous (unlike a bare clock), so it can be resolved to a
# concrete next-occurrence instead of dropped to None.
_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
# Match only genuine weekday tokens (3-letter abbrev OR full name), ``\b``-bounded
# both ends so a word merely STARTING with a day prefix (``month``→mon,
# ``friendly``→fri) can't false-match. Separator allows a comma ("Monday, 9am").
# The map key is the captured token's first 3 letters.
_WEEKDAY_CLOCK_RE = re.compile(
    r"reset[a-z ]*?\b(?P<wd>mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b[,\s]+(?:at\s*)?"
    r"(?P<h>\d{1,2})(?::(?P<min>\d{2}))?\s*(?P<ap>am|pm)",
    re.IGNORECASE,
)
# A trailing IANA zone hint, e.g. "resets 4:10am (America/Los_Angeles)". The CC CLI
# renders the reset time in the ACCOUNT's local zone, not the container's — so a
# bare wall-clock parsed in ``now``'s tz (UTC on a server) lands hours off. Case
# preserved: IANA zone keys are case-sensitive.
_TZ_HINT_RE = re.compile(r"\(([A-Za-z]+(?:/[A-Za-z0-9_+-]+)+)\)")


def _extract_tz_hint(text: str) -> tzinfo | None:
    """A trailing ``(Area/Location)`` zone hint as a tzinfo, or None.

    Best-effort: an absent or unresolvable zone (no tzdata, bad name) returns
    None so the caller falls back to naive parsing in ``now``'s tz — never
    raises, never worse than pre-hint behavior."""
    m = _TZ_HINT_RE.search(text)
    if not m:
        return None
    try:
        return ZoneInfo(m.group(1))
    except Exception:
        return None


def _clock_24h(h: str, minute: str | None, ap: str) -> tuple[int, int]:
    """(hour_group, minute_group, am/pm) → 24h (hour, minute)."""
    hour = int(h) % 12
    if ap.lower() == "pm":
        hour += 12
    return hour, int(minute or 0)


def _account_ref(now: datetime, text: str) -> tuple[datetime, tzinfo | None]:
    """``now`` shifted into the account's zone if the prose names one, else
    ``now`` unchanged. Returns ``(ref, tz)``; a wall-clock built on ``ref`` is
    handed back to ``now``'s tz only when ``tz`` is not None."""
    tz = _extract_tz_hint(text)
    return (now.astimezone(tz), tz) if tz is not None else (now, None)


def _reset_from_prose(text: str, now: datetime, limit_kind: str) -> datetime | None:
    """Parse a reset time from CC's prose. Relative durations are unambiguous;
    a weekday-bearing wall-clock ("Monday 9am") resolves to the next occurrence;
    a BARE wall-clock is day-ambiguous for a WEEKLY limit → None. A wall-clock
    carrying an ``(IANA/Zone)`` hint is resolved in that zone (the account's
    local zone) and returned in ``now``'s tz."""
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

    # Weekday wall-clock: "resets Monday 9am" — a named day is unambiguous even
    # for a weekly cap, so resolve the NEXT occurrence of that weekday/time
    # (account-zone-aware) rather than dropping to the cadence floor. Checked
    # before the bare-clock branch since the bare regex would also match here.
    w = _WEEKDAY_CLOCK_RE.search(low)
    if w:
        hour, minute = _clock_24h(w.group("h"), w.group("min"), w.group("ap"))
        target_wd = _WEEKDAYS[w.group("wd").lower()[:3]]
        ref, tz = _account_ref(now, text)
        days_ahead = (target_wd - ref.weekday()) % 7
        candidate = ref.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(
            days=days_ahead
        )
        if candidate <= ref:  # today-but-already-past → next week
            candidate += timedelta(days=7)
        return candidate.astimezone(now.tzinfo) if tz is not None else candidate

    # Bare wall-clock: "resets 5pm" / "resets at 11:30 am" / "resets 4:10am (America/Los_Angeles)".
    c = _CLOCK_RE.search(low)
    if c:
        if limit_kind == WEEKLY:
            # Which day? Unknowable from a bare clock time — don't guess.
            return None
        hour, minute = _clock_24h(c.group("h"), c.group("min"), c.group("ap"))
        # Resolve the wall-clock in the account's zone when the CLI names it
        # (search the ORIGINAL-case text — zone keys are case-sensitive), then
        # hand back a datetime in ``now``'s tz. No hint → parse in ``now``'s tz
        # exactly as before (unchanged behavior).
        ref, tz = _account_ref(now, text)
        candidate = ref.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= ref:
            candidate += timedelta(days=1)
        return candidate.astimezone(now.tzinfo) if tz is not None else candidate
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
