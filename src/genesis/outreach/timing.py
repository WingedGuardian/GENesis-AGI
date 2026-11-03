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
There is no "hand whatever is left to dateparser" stage: the parser guesses
at inputs like ``9p`` or ``in 5``, and a confident wrong answer delivers at
the wrong time (or the year 1430). The caller is an LLM that can retry, so a
refusal it can correct beats a guess it cannot see.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

# "9am", "9:30pm", "9a", "9p", "9a.m." — meridiem REQUIRED.
_CLOCK_MERIDIEM_RE = re.compile(
    r"^(?:at\s+)?(\d{1,2})(?::(\d{1,2}))?\s*([ap])(?:\.?m\.?)?$", re.IGNORECASE
)
# "14:00", "9:30", "at 9" — 24-hour clock, meridiem absent.
_CLOCK_24_RE = re.compile(r"^(?:at\s+)?(\d{1,2})(?::(\d{2}))?$", re.IGNORECASE)

# One <amount><unit> pair inside an elapsed phrase: "2 hours", "120m", "an hour",
# the "1h" and "30m" halves of "1h30m". "days"/"weeks" are deliberately NOT
# units: an elapsed duration is added to the UTC clock, while a calendar day
# keeps the local clock time — `in 2 days` is resolved by the calendar stage.
_ELAPSED_PART_RE = re.compile(
    r"(\d+(?:\.\d+)?|an?)\s*"
    r"(h(?:ours?|rs?)?|m(?:in(?:ute)?s?)?|s(?:ec(?:ond)?s?)?)",
    re.IGNORECASE,
)
_ELAPSED_HEAD_RE = re.compile(r"^(?:in|after)\s+", re.IGNORECASE)
_ELAPSED_TAIL_RE = re.compile(r"\s*(?:later|from\s+now)$", re.IGNORECASE)

# A word that makes a phrase CALENDAR-shaped: only these reach dateparser.
# Anything the parser would guess at instead — "at 9", "9a", "in 5", "1430" —
# contains none of them and is refused before it gets there.
_CALENDAR_WORD_RE = re.compile(
    r"\b(today|tomorrow|tonight|days?|weeks?|"
    r"(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|"
    r"dec(?:ember)?)\b",
    re.IGNORECASE,
)
# The clock a calendar phrase itself names ("tomorrow 9am" → 09:00) — the
# fold check runs on THAT wall time, not on whatever dateparser returned,
# because dateparser silently shifts a gap/fold wall time to the nearest
# real one.
_PHRASE_CLOCK_MERIDIEM_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m?\.?\b", re.IGNORECASE
)
_PHRASE_CLOCK_24_RE = re.compile(r"\b(\d{1,2}):(\d{2})\b")
# dateparser cannot read "next <weekday>": it is parsed as the bare weekday
# (the upcoming occurrence) plus seven days.
_NEXT_WEEKDAY_RE = re.compile(
    r"\bnext\s+(?=(?:mon|tues|wednes|thurs|fri|satur|sun)day\b)", re.IGNORECASE
)
# "in N days/weeks" keeps the local CLOCK time (calendar days, not +48h
# absolute — across a DST change the wall time is what the caller meant).
_IN_DAYS_RE = re.compile(
    r"^in\s+(\d+(?:\.\d+)?)\s+(days?|weeks?)\.?$", re.IGNORECASE
)
# A phrase that names its own zone is resolved in that zone — the local
# wall-time fold check must not second-guess it.
_ZONE_TOKEN_RE = re.compile(
    r"(?:\b(?:utc|gmt|[acehmp][sd]t|ak[sd]t)\b|[+-]\d{1,2}(?::?\d{2})?\b)",
    re.IGNORECASE,
)

_GUIDANCE = (
    "use an ISO-8601 time, a clock time like '9am', an elapsed time like "
    "'in 2 hours', or a calendar phrase like 'tomorrow 9am'"
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
    seconds = 0.0
    for num, unit in _ELAPSED_PART_RE.findall(s):
        n = 1.0 if num.lower() in ("a", "an") else float(num)
        u = unit.lower()
        if u.startswith("h"):
            seconds += n * 3600
        elif u.startswith("m"):
            seconds += n * 60
        else:
            seconds += n
    return timedelta(seconds=seconds)


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

    tzname = tz
    if tzname is None:
        from genesis.env import user_timezone

        tzname = user_timezone()
    try:
        local_tz = zoneinfo.ZoneInfo(tzname)
    except (zoneinfo.ZoneInfoNotFoundError, KeyError, ValueError) as exc:
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
            dt = datetime.fromisoformat(raw)
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
        m = _CLOCK_MERIDIEM_RE.match(raw)
        if m:
            meridiem = (m.group(3) or "").lower()
        else:
            m = _CLOCK_24_RE.match(raw)
            meridiem = ""
        if m:
            hour = int(m.group(1))
            minute = int(m.group(2) or 0)
            if meridiem:
                if not 1 <= hour <= 12:
                    raise TimingParseError(
                        f"could not interpret preferred_timing '{raw}'; {_GUIDANCE}"
                    )
                hour = hour % 12 + (12 if meridiem == "p" else 0)
            if hour > 23 or minute > 59:
                raise TimingParseError(
                    f"could not interpret preferred_timing '{raw}'; {_GUIDANCE}"
                )
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
            return base_utc + delta

        # 4. Calendar phrase → dateparser, but only when the phrase holds a
        #    calendar word, and only with the fold/horizon checks below.
        #    "in N days/weeks" is calendar arithmetic on the local clock — a
        #    DST change inside the window must not move the wall time.
        in_days = _IN_DAYS_RE.match(raw)
        if in_days:
            n = float(in_days.group(1))
            days = n * (7 if in_days.group(2).lower().startswith("w") else 1)
            wall = base_utc.astimezone(local_tz).replace(tzinfo=None) + timedelta(
                days=days
            )
            instant = _one_local_instant(wall, local_tz, tzname, raw)
            if instant > base_utc + timedelta(days=366):
                raise TimingParseError(
                    f"preferred_timing '{raw}' resolves more than a year "
                    f"out — {_GUIDANCE}"
                )
            return instant
        if _CALENDAR_WORD_RE.search(raw):
            try:
                import dateparser
            except ImportError as exc:
                raise TimingParseError(
                    "preferred_timing: natural-language resolution needs the "
                    "dateparser package, which is not installed"
                ) from exc
            parse_raw = raw
            if _NEXT_WEEKDAY_RE.search(raw):
                parse_raw = _NEXT_WEEKDAY_RE.sub("", raw, count=1)
            # A phrase naming its own zone resolves in that zone — dateparser
            # ignores zone tokens under the TIMEZONE setting, so the token is
            # stripped, the remainder is parsed naive relative to now in the
            # named zone, and that zone is attached back via _fold_instants.
            # Abbreviated zones are ambiguous everywhere (CST is four
            # different offsets); only utc/gmt and numeric offsets qualify.
            zone_m = _ZONE_TOKEN_RE.search(raw)
            named_tz = None
            if zone_m:
                token = zone_m.group(0).lower()
                if token in ("utc", "gmt"):
                    named_tz = UTC
                elif token[0] in "+-":
                    off = token[1:].replace(":", "")
                    if len(off) <= 2:
                        # "+5"/"-05" name whole hours, not minutes.
                        hours, mins = int(off), 0
                    else:
                        hours, mins = int(off[:-2]), int(off[-2:])
                    named_tz = timezone(
                        timedelta(hours=hours, minutes=mins)
                        * (1 if token[0] == "+" else -1)
                    )
                else:
                    raise TimingParseError(
                        f"preferred_timing '{raw}' names the ambiguous zone "
                        f"'{zone_m.group(0)}' — use a numeric UTC offset"
                    )
                parse_raw = _ZONE_TOKEN_RE.sub("", parse_raw).strip()
            parsed = dateparser.parse(
                parse_raw,
                settings={
                    "PREFER_DATES_FROM": "future",
                    "RELATIVE_BASE": base_utc.astimezone(
                        named_tz or local_tz
                    ).replace(tzinfo=None),
                    "TIMEZONE": tzname,
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DAY_OF_MONTH": "first",
                },
            )
            if parsed is not None and parsed.tzinfo is not None:
                # The clock the phrase names is searched in parse_raw, NOT
                # raw — a zone token's digits ("tomorrow +05:30") would
                # otherwise read as a 05:30 clock and override the wall time.
                meridiem = None
                clock = _PHRASE_CLOCK_MERIDIEM_RE.search(parse_raw)
                if clock:
                    meridiem = clock.group(3).lower()
                else:
                    clock = _PHRASE_CLOCK_24_RE.search(parse_raw)
                if clock:
                    h = int(clock.group(1))
                    minute = int(clock.group(2) or 0)
                    if meridiem is not None:
                        if not 1 <= h <= 12:
                            raise TimingParseError(
                                f"could not interpret preferred_timing "
                                f"'{raw}'; {_GUIDANCE}"
                            )
                        h = h % 12 + (12 if meridiem == "p" else 0)
                    if h > 23 or minute > 59:
                        raise TimingParseError(
                            f"could not interpret preferred_timing "
                            f"'{raw}'; {_GUIDANCE}"
                        )
                if named_tz is not None:
                    # The parsed wall fields are relative to now in the named
                    # zone; a "next <weekday>" rewrite adds seven days on that
                    # zone's wall clock before the instant is taken.
                    wall = parsed.replace(tzinfo=None)
                    if _NEXT_WEEKDAY_RE.search(raw):
                        wall += timedelta(days=7)
                    if clock:
                        wall = wall.replace(
                            hour=h, minute=minute, second=0, microsecond=0
                        )
                    instant = _one_local_instant(
                        wall, named_tz, zone_m.group(0), raw
                    )
                else:
                    # The fold check runs on the wall time the PHRASE named,
                    # not on dateparser's result — the parser silently shifts
                    # a gap/fold wall time to the nearest real one, and that
                    # shift is exactly the guess this check exists to refuse.
                    naive = parsed.astimezone(local_tz).replace(tzinfo=None)
                    if parse_raw != raw:
                        naive += timedelta(days=7)
                    if clock:
                        wall = naive.replace(
                            hour=h, minute=minute, second=0, microsecond=0
                        )
                    else:
                        wall = naive
                    instant = _one_local_instant(wall, local_tz, tzname, raw)
                if instant <= base_utc:
                    raise TimingParseError(
                        f"preferred_timing '{raw}' resolves to the past — "
                        f"{_GUIDANCE}"
                    )
                if instant > base_utc + timedelta(days=366):
                    raise TimingParseError(
                        f"preferred_timing '{raw}' resolves more than a year "
                        f"out — {_GUIDANCE}"
                    )
                return instant
    except OverflowError as exc:
        raise TimingParseError(
            f"preferred_timing '{raw}' resolves out of range — {_GUIDANCE}"
        ) from exc

    raise TimingParseError(
        f"could not interpret preferred_timing '{raw}'; {_GUIDANCE}"
    )
