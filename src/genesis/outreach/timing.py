"""Resolve ``preferred_timing`` values for scheduled outreach.

Single source of truth for turning a caller-supplied delivery time into an
aware UTC datetime. The pending_outreach drain compares ``deliver_after``
lexicographically against a UTC ISO ``now`` string, so anything stored MUST be
``dt.astimezone(UTC).isoformat()``: a verbatim NL string sorts after every
``2...`` ISO timestamp and never fires, and an ISO timestamp with a non-UTC
offset compares incorrectly. Both failure modes are silent — the row sits
undeliverable until the 24h age-out drops it.

Resolution is an ALLOWLIST, tried in order — ISO-8601, a clock time, an
elapsed time, then a calendar phrase — and anything unmatched is refused.
Each stage matches the WHOLE string against a grammar, and there is no parser
behind them to guess: a general date parser read ``at 9`` as a month, ``9a`` as
a year and ``UTC+5`` as UTC, each reported as success. The caller is an LLM that
can retry, so a refusal it can correct beats a guess it cannot see.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo

# "9am", "9:30pm", "9a", "9p", "9a.m." — meridiem REQUIRED.
_CLOCK_MERIDIEM_RE = re.compile(
    r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*([ap])(?:\.?m\.?)?$", re.IGNORECASE | re.ASCII
)
# "14:00", "9:30", "at 9" — 24-hour clock, meridiem absent.
_CLOCK_24_RE = re.compile(r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?$", re.IGNORECASE | re.ASCII)

# One <amount><unit> pair inside an elapsed phrase: "2 hours", "120m", "an hour",
# the "1h" and "30m" halves of "1h30m". "days"/"weeks" are deliberately NOT
# units: an elapsed duration is added to the UTC clock, while a calendar day
# keeps the local clock time — `in 2 days` is resolved by the calendar stage.
_ELAPSED_PART_RE = re.compile(
    r"(\d+(?:\.\d+)?\s*|\ban?\s+(?=hour|hr|min|sec))"
    r"(h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)(?![a-z])",
    re.IGNORECASE | re.ASCII,
)
_ELAPSED_HEAD_RE = re.compile(r"^(?:in|after)\s+", re.IGNORECASE | re.ASCII)
_ELAPSED_TAIL_RE = re.compile(r"\s*(?:later|from\s+now)$", re.IGNORECASE | re.ASCII)

# The calendar grammar, matched whole:
#   [on] <day> [[at] <clock>] [<zone>]     "tomorrow 9am", "next friday at 3pm UTC"
#   [at] <clock> [on] <day> [<zone>]       "3pm tomorrow", "noon on friday"
#   [at] <clock> <zone>                    "9am UTC"
#   in N days/weeks, N days from now/later "in 2 days", "a week from now"
_WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5, "sunday": 6, "sun": 6,
}  # fmt: skip
_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4,
    "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11,
    "nov": 11, "december": 12, "dec": 12,
}  # fmt: skip
_WEEKDAY = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DOM = r"\d{1,2}(?:st|nd|rd|th)?"
_DAY = (
    r"(?:today|tonight|tomorrow|(?:the\s+)?day\s+after\s+tomorrow"
    rf"|(?:(?:next|this)\s+)?(?:{_WEEKDAY})"
    rf"|(?:{_MONTH})\s+{_DOM}(?:,?\s+\d{{4}})?"
    rf"|{_DOM}\s+(?:of\s+)?(?:{_MONTH})(?:,?\s+\d{{4}})?)"
)
_CLOCK = r"(?:noon|midnight|\d{1,2}(?::\d{2})?(?:\s*[ap](?:\.?m\.?)?)?)"
_ZONE = r"(?:(?:utc|gmt)(?:\s*[+-]\d{1,2}(?::?\d{2})?)?|[+-]\d{1,2}(?::?\d{2})?)"
_CAL_DAY_FIRST_RE = re.compile(
    rf"^(?:on\s+)?(?P<day>{_DAY})(?:\s+(?:at\s+)?(?P<clock>{_CLOCK}))?(?:\s+(?P<zone>{_ZONE}))?$",
    re.IGNORECASE | re.ASCII,
)
_CAL_CLOCK_FIRST_RE = re.compile(
    rf"^(?:at\s+)?(?P<clock>{_CLOCK})\s+(?:on\s+)?(?P<day>{_DAY})(?:\s+(?P<zone>{_ZONE}))?$",
    re.IGNORECASE | re.ASCII,
)
_CAL_CLOCK_ZONE_RE = re.compile(
    rf"^(?:at\s+)?(?P<clock>{_CLOCK})\s+(?P<zone>{_ZONE})$", re.IGNORECASE | re.ASCII
)
_CAL_SPAN_RE = re.compile(
    r"^(?:in\s+(?P<a>\d+|an?)\s+(?P<u>days?|weeks?)"
    r"|(?P<b>\d+|an?)\s+(?P<v>days?|weeks?)\s+(?:from\s+now|later))$",
    re.IGNORECASE | re.ASCII,
)
_HORIZON = timedelta(days=366)
_MAX_CHARS = 64
_EVENING_HOUR = 17  # "tonight" below this is not tonight

_GUIDANCE = (
    "use an ISO-8601 time, a clock time like '9am', an elapsed time like "
    "'in 2 hours', or a day and clock like 'tomorrow 9am', 'next friday at 3pm', "
    "'june 20 3pm UTC' or 'tomorrow noon'"
)


class TimingParseError(ValueError):
    """Raised when ``preferred_timing`` cannot be resolved to a datetime."""


def _fold_instants(naive: datetime, local_tz) -> list[datetime]:
    """UTC instants a naive local wall time maps to, in chronological order.

    Tests both DST folds and keeps an instant only when it round-trips back
    to the same wall time. Normally one entry; two on the autumn rollback
    (the wall time exists twice); zero in the spring-forward gap (the wall
    time never exists).
    """
    instants: list[datetime] = []
    for fold in (0, 1):
        instant = naive.replace(tzinfo=local_tz, fold=fold).astimezone(UTC)
        if (
            instant.astimezone(local_tz).replace(tzinfo=None) == naive
            and instant not in instants
        ):
            instants.append(instant)
    return sorted(instants)


def _one_local_instant(wall: datetime, local_tz, tzname: str, raw: str) -> datetime:
    """The single UTC instant a local wall time names, or a refusal.

    A wall time in a DST gap does not exist, and one in a fold exists twice —
    resolving either by picking a side delivers at a time the caller did not
    ask for, so both are refused with an instruction to pass an offset.
    """
    instants = _fold_instants(wall, local_tz)
    if len(instants) == 1:
        return instants[0]
    kind = "does not exist" if not instants else "is ambiguous"
    raise TimingParseError(
        f"preferred_timing '{raw}': the local time {wall:%H:%M} on "
        f"{wall:%Y-%m-%d} {kind} in timezone {tzname} (a DST "
        f"{'gap' if not instants else 'fold'}) — pass a UTC-offset-aware "
        "ISO value instead"
    )


def _elapsed_delta(text: str) -> timedelta | None:
    """The duration an elapsed phrase names, or None when it is not one.

    Accepts an optional ``in``/``after`` lead and ``later``/``from now`` tail,
    one trailing period, and h/m/s units (``1h30m``, ``1.5 hours``, ``an
    hour``). Anything left over — number words, a bare count with no unit, a
    negative sign — means the string is not elapsed time.
    """
    s = text.strip()
    if s.endswith("."):
        s = s[:-1].strip()
    s = _ELAPSED_HEAD_RE.sub("", s, count=1)
    s = _ELAPSED_TAIL_RE.sub("", s, count=1)
    matches = list(_ELAPSED_PART_RE.finditer(s))
    if not matches:
        return None
    # Text between matched durations may be separators or the connector
    # "and"; the leading and trailing segments may only be separators, so a
    # dangling "in 2 hours and" refuses instead of silently scheduling.
    for i, match in enumerate(matches):
        seg = s[matches[i - 1].end() if i else 0 : match.start()].strip(" ,")
        if seg != "" and (seg.lower() != "and" or i == 0):
            return None
    if s[matches[-1].end() :].strip(" ,") != "":
        return None
    parts = _ELAPSED_PART_RE.findall(s)
    if len({unit[0].lower() for _, unit in parts}) != len(parts):
        return None
    seconds = 0.0
    for num, unit in parts:
        num = num.strip()
        n = 1.0 if num.lower() in ("a", "an") else float(num)
        u = unit.lower()
        if u.startswith("h"):
            seconds += n * 3600
        elif u.startswith("m"):
            seconds += n * 60
        else:
            seconds += n
    return timedelta(seconds=seconds)


def _refuse(raw: str) -> TimingParseError:
    return TimingParseError(f"could not interpret preferred_timing '{raw}'; {_GUIDANCE}")


def _parse_clock(text: str, raw: str) -> tuple[int, int]:
    """(hour, minute) of a grammar clock. A bare number reads as the 24-hour hour."""
    s = text.strip().lower()
    if s == "noon":
        return 12, 0
    if s == "midnight":
        return 0, 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(?:([ap])(?:\.?m\.?)?)?", s, re.ASCII)
    if not m:
        raise _refuse(raw)
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    if m.group(3):
        if not 1 <= hour <= 12:
            raise _refuse(raw)
        hour = hour % 12 + (12 if m.group(3) == "p" else 0)
    if hour > 23 or minute > 59:
        raise _refuse(raw)
    return hour, minute


def _parse_zone(text: str, raw: str) -> tzinfo:
    """A fixed zone: UTC, GMT, either with an offset, or a bare offset."""
    s = re.sub(r"\s+", "", text.lower(), flags=re.ASCII).removeprefix("utc").removeprefix("gmt")
    if not s:
        return UTC
    digits = s[1:].replace(":", "")
    hours, minutes = (int(digits), 0) if len(digits) <= 2 else (int(digits[:-2]), int(digits[-2:]))
    if hours > 14 or minutes > 59:
        raise TimingParseError(f"preferred_timing '{raw}' has an impossible UTC offset")
    return timezone(timedelta(hours=hours, minutes=minutes) * (1 if s[0] == "+" else -1))


def _date_in_grammar(day: str, today: date, raw: str) -> date | None:
    """The date a <day> names, or None when it is a weekday, which needs the clock."""
    s = re.sub(r"\s+", " ", day.strip().lower())
    if s in ("today", "tonight"):
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    if s.endswith("after tomorrow"):
        return today + timedelta(days=2)
    m = re.fullmatch(rf"(?:(?P<mon1>{_MONTH}) (?P<d1>\d+)|(?P<d2>\d+)(?:st|nd|rd|th)? (?:of )?(?P<mon2>{_MONTH}))"
                     r"(?:st|nd|rd|th)?(?:,? (?P<y>\d{4}))?", s, re.ASCII)  # fmt: skip
    if not m:
        return None
    month = _MONTHS[m.group("mon1") or m.group("mon2")]
    number = int(m.group("d1") or m.group("d2"))
    try:
        if m.group("y"):
            return date(int(m.group("y")), month, number)
        named = date(today.year, month, number)
        return named if named >= today else date(today.year + 1, month, number)
    except ValueError as exc:
        raise _refuse(raw) from exc


def _calendar_instant(raw: str, base_utc: datetime, local_tz: tzinfo, tzname: str) -> datetime | None:
    """The instant a calendar phrase names, or None when the grammar does not match."""
    text = raw.strip().removesuffix(".").strip()

    span = _CAL_SPAN_RE.match(text)
    if span:
        count, unit = (span.group("a") or span.group("b")), (span.group("u") or span.group("v"))
        n = 1 if count.lower() in ("a", "an") else int(count)
        if n < 1:
            raise _refuse(raw)
        days = n * (7 if unit.lower().startswith("w") else 1)
        wall = base_utc.astimezone(local_tz).replace(tzinfo=None) + timedelta(days=days)
        return _one_local_instant(wall, local_tz, tzname, raw)

    m = _CAL_DAY_FIRST_RE.match(text)
    if not m:
        m = _CAL_CLOCK_FIRST_RE.match(text) or _CAL_CLOCK_ZONE_RE.match(text)
        # Leading a phrase, a bare number needs "at": "3 friday" could be a date.
        if m and m.group("clock").isdigit() and not text.lower().startswith("at"):
            return None
    if not m:
        return None
    groups = m.groupdict()
    day, clock, zone = groups.get("day"), groups.get("clock"), groups.get("zone")

    zone_tz = _parse_zone(zone, raw) if zone else local_tz
    zone_name = zone.strip() if zone else tzname
    local_now = base_utc.astimezone(zone_tz)
    today = local_now.date()
    hm = _parse_clock(clock, raw) if clock else None

    if day is None:  # "<clock> <zone>": the next occurrence in that zone.
        for extra in (0, 1, 2):
            wall = datetime.combine(today + timedelta(days=extra), time(*hm))
            future = [u for u in _fold_instants(wall, zone_tz) if u > base_utc]
            if future:
                return future[0]
        raise _refuse(raw)

    key = re.sub(r"\s+", " ", day.strip().lower())
    bare = bool(clock) and clock.strip().isdigit()
    # Which night "tomorrow midnight" means is a day's difference.
    if clock and clock.strip().lower() == "midnight":
        raise _refuse(raw)
    # A clockless day with a zone reads like arithmetic: "tomorrow -5".
    if zone and hm is None:
        raise _refuse(raw)
    if key == "tonight":
        if hm is None:
            raise _refuse(raw)
        if bare and 1 <= hm[0] <= 11:
            hm = (hm[0] + 12, hm[1])  # "tonight at 9" is 21:00
        if hm[0] < _EVENING_HOUR:
            raise _refuse(raw)
    elif bare and 1 <= hm[0] <= 12:
        # "friday at 5" is a guess between 05:00 and 17:00.
        raise _refuse(raw)

    named = _date_in_grammar(day, today, raw)
    if named is not None:
        if hm is None:
            # Only a relative day has an obvious clock: this time tomorrow.
            if key == "today" or key == "tonight" or _MONTHS.get(key.split()[0]) or key[0].isdigit():
                raise _refuse(raw)
            hm = (local_now.hour, local_now.minute)
        return _one_local_instant(datetime.combine(named, time(*hm)), zone_tz, zone_name, raw)

    # A weekday needs its clock: on its own it names a whole day.
    if hm is None:
        raise _refuse(raw)
    words = key.split()
    weekday = _WEEKDAYS[words[-1]]
    if words[0] == "next":
        # The named day of the next calendar week, Monday first.
        monday = today - timedelta(days=today.weekday()) + timedelta(days=7)
        return _one_local_instant(
            datetime.combine(monday + timedelta(days=weekday), time(*hm)), zone_tz, zone_name, raw
        )
    if words[0] == "this" and weekday < today.weekday():
        raise _refuse(raw)  # this week's has gone, and next week's is not "this"
    ahead = (weekday - today.weekday()) % 7
    instant = _one_local_instant(
        datetime.combine(today + timedelta(days=ahead), time(*hm)), zone_tz, zone_name, raw
    )
    if instant <= base_utc:  # later today has passed
        if words[0] == "this":  # "this monday" cannot mean next week's
            raise _refuse(raw)
        instant = _one_local_instant(
            datetime.combine(today + timedelta(days=ahead + 7), time(*hm)), zone_tz, zone_name, raw
        )
    return instant


def resolve_preferred_timing(
    value: str | None,
    *,
    now: datetime | None = None,
    tz: str | None = None,
) -> datetime:
    """Resolve ``value`` to an aware UTC datetime or raise TimingParseError.

    ``now``/``tz`` are injectable for tests; when ``tz`` is None the resolution
    timezone comes from ``genesis.env.user_timezone()``.
    """
    import zoneinfo

    raw = (value or "").strip()
    if not raw:
        raise TimingParseError("preferred_timing is empty")
    if len(raw) > _MAX_CHARS:
        raise TimingParseError(
            f"preferred_timing is longer than {_MAX_CHARS} characters; {_GUIDANCE}"
        )

    tzname = tz
    if tzname is None:
        from genesis.env import user_timezone

        tzname = user_timezone()
    try:
        local_tz = zoneinfo.ZoneInfo(tzname)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError, ValueError, OSError) as exc:
        raise TimingParseError(
            f"preferred_timing '{raw}': unusable timezone {tzname!r}"
        ) from exc

    base_utc = now or datetime.now(UTC)
    if base_utc.tzinfo is None:
        base_utc = base_utc.replace(tzinfo=UTC)
    base_utc = base_utc.astimezone(UTC)

    try:
        # 1. ISO-8601. Aware: that instant (a PAST value stays past — the
        #    caller treats it as deliver-now, unchanged). Naive: the user's
        #    timezone, but only when the wall time maps to exactly one instant.
        try:
            dt = datetime.fromisoformat(raw) if re.match(r"^\d{4}-.*[T ]", raw, re.ASCII) else None
        except (ValueError, TypeError):
            dt = None
        if dt is not None:
            if dt.tzinfo is None:
                return _one_local_instant(dt, local_tz, tzname, raw)
            return dt.astimezone(UTC)

        # 2. Clock time → the next local occurrence. Both meridiem and 24-hour
        #    forms; a bare number reads as the hour. The search runs three
        #    days because a wall time inside tomorrow's DST gap produces no
        #    instant that day and must land on the day after instead.
        m = _CLOCK_MERIDIEM_RE.match(raw) or _CLOCK_24_RE.match(raw)
        if m:
            hour, minute = _parse_clock(re.sub(r"^at\s+", "", raw, flags=re.IGNORECASE), raw)
            local_now = base_utc.astimezone(local_tz)
            for extra_days in (0, 1, 2):
                day = (local_now + timedelta(days=extra_days)).date()
                wall = datetime(day.year, day.month, day.day, hour, minute)
                future = [u for u in _fold_instants(wall, local_tz) if u > base_utc]
                if future:
                    return future[0]
            raise TimingParseError(
                f"could not interpret preferred_timing '{raw}'; {_GUIDANCE}"
            )

        # 3. Elapsed time → added to the UTC clock. A duration IS a duration —
        #    adding it to the LOCAL wall clock instead loses or gains an hour
        #    whenever a clock change falls inside the window.
        delta = _elapsed_delta(raw)
        if delta is not None:
            if delta <= timedelta(0):
                raise _refuse(raw)
            if delta > _HORIZON:
                raise TimingParseError(
                    f"preferred_timing '{raw}' resolves more than a year out — {_GUIDANCE}"
                )
            return base_utc + delta

        # 4. Calendar phrase, matched whole against the grammar above. A day
        #    keeps the local clock across a DST change, and a wall time in a
        #    gap or fold is refused rather than moved.
        instant = _calendar_instant(raw, base_utc, local_tz, tzname)
        if instant is not None:
            if instant <= base_utc:
                raise TimingParseError(
                    f"preferred_timing '{raw}' resolves to the past — {_GUIDANCE}"
                )
            if instant > base_utc + _HORIZON:
                raise TimingParseError(
                    f"preferred_timing '{raw}' resolves more than a year out — {_GUIDANCE}"
                )
            return instant
    except OverflowError as exc:
        raise TimingParseError(
            f"preferred_timing '{raw}' resolves out of range — {_GUIDANCE}"
        ) from exc

    raise TimingParseError(
        f"could not interpret preferred_timing '{raw}'; {_GUIDANCE}"
    )
