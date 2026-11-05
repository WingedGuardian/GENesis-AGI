"""Tests for genesis.outreach.timing.resolve_preferred_timing.

``now`` and ``tz`` are injected explicitly so every case is deterministic —
no dependence on the host clock or install timezone.
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC, datetime, timedelta

import pytest

from genesis.outreach.timing import (
    TimingParseError,
    resolve_preferred_timing,
)

# A fixed mid-day UTC instant: 2026-03-14 15:00 UTC is 10:00 in America/Chicago
# (CDT, DST already started 2026-03-08).
NOW = datetime(2026, 3, 14, 15, 0, 0, tzinfo=UTC)
# The rework spec's anchor: 10:00 CDT on 2026-06-10.
SPEC_NOW = datetime(2026, 6, 10, 15, 0, 0, tzinfo=UTC)
# Spring-forward morning: 01:30 CST on 2026-03-08 (the 02:00→03:00 jump is ahead).
SPRING_FORWARD = datetime(2026, 3, 8, 7, 30, 0, tzinfo=UTC)
# Autumn rollback morning: 00:30 CDT on 2026-11-01 (the 02:00→01:00 fold is ahead).
FALL_BACK = datetime(2026, 11, 1, 5, 30, 0, tzinfo=UTC)

CHICAGO = "America/Chicago"
PHOENIX = "America/Phoenix"  # no DST — the non-DST zone required by the spec


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _local_wall(dt: datetime, tz: str) -> str:
    return dt.astimezone(zoneinfo.ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


# ── ISO-8601 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tz", ["UTC", CHICAGO])
def test_iso_aware_unchanged(tz):
    raw = "2026-03-20T18:30:00+00:00"
    assert _iso(resolve_preferred_timing(raw, now=NOW, tz=tz)) == raw


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        ("UTC", "2026-03-20T18:30:00+00:00"),
        # 18:30 naive Chicago (CDT, UTC-5) → 23:30 UTC.
        (CHICAGO, "2026-03-20T23:30:00+00:00"),
    ],
)
def test_iso_naive_uses_user_timezone(tz, expected):
    raw = "2026-03-20T18:30:00"
    assert _iso(resolve_preferred_timing(raw, now=NOW, tz=tz)) == expected


def test_iso_past_aware_unchanged():
    """A past ISO value keeps today's behaviour — not an error."""
    raw = "2020-01-01T12:00:00+00:00"
    assert _iso(resolve_preferred_timing(raw, now=NOW, tz="UTC")) == raw


def test_naive_iso_ambiguous_across_dst_raises():
    """A naive ISO local time that exists twice (rollback) cannot pick a side
    silently — it is refused like other unresolvable input."""
    now = datetime(2026, 11, 1, 6, 0, 0, tzinfo=UTC)
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(
            "2026-11-01T01:30:00", now=now, tz=CHICAGO
        )


def test_naive_iso_in_dst_gap_raises():
    """02:30 on the spring-forward day never exists in Chicago — refuse, never
    silently shift to 03:30."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(
            "2026-03-08T02:30:00", now=SPRING_FORWARD - timedelta(hours=12), tz=CHICAGO
        )


def test_iso_out_of_range_raises_timing_error():
    """datetime.max overflows the UTC conversion — a TimingParseError, never a
    bare OverflowError escaping the tool's error handling."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing("9999-12-31T23:59:59", now=NOW, tz=CHICAGO)


# ── Clock time ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected_local"),
    [
        # From 10:00 CDT on 2026-06-10 — the spec's table.
        ("at 9", "2026-06-11 09:00"),   # next 09:00, not September
        ("9a", "2026-06-11 09:00"),     # next 09:00, not the year 2035
        ("9p", "2026-06-10 21:00"),     # next 21:00, not 9 minutes out
        ("9am", "2026-06-11 09:00"),
        ("9:30pm", "2026-06-10 21:30"),
        ("14:00", "2026-06-10 14:00"),
    ],
)
def test_clock_time_spec_table(raw, expected_local):
    got = resolve_preferred_timing(raw, now=SPEC_NOW, tz=CHICAGO)
    assert _local_wall(got, CHICAGO) == expected_local


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        (CHICAGO, "2026-03-15T14:00:00+00:00"),
        ("UTC", "2026-03-15T09:00:00+00:00"),
    ],
)
def test_bare_clock_time_prefers_future(tz, expected):
    assert _iso(resolve_preferred_timing("9am", now=NOW, tz=tz)) == expected


@pytest.mark.parametrize("bad", ["25:00", "9:99", "13am", "0:45pm"])
def test_invalid_clock_raises(bad):
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(bad, now=NOW, tz="UTC")


def test_bare_clock_dst_rollback_picks_second_occurrence():
    """On the autumn rollback morning, 01:30 exists twice in Chicago. From the
    first 01:45 (06:45 UTC), "1:30am" resolves to the still-future SECOND
    occurrence (07:30 UTC, 45 min away) — not next day's."""
    now = datetime(2026, 11, 1, 6, 45, 0, tzinfo=UTC)
    assert _iso(
        resolve_preferred_timing("1:30am", now=now, tz=CHICAGO)
    ) == "2026-11-01T07:30:00+00:00"


def test_bare_clock_in_dst_gap_skips_to_next_day():
    """02:30 never exists on the 2026-03-08 spring-forward in Chicago, so a
    "2:30am" asked that morning resolves to the next day's occurrence."""
    now = datetime(2026, 3, 8, 9, 0, 0, tzinfo=UTC)  # 03:00 CST, post-gap
    assert _iso(
        resolve_preferred_timing("2:30am", now=now, tz=CHICAGO)
    ) == "2026-03-09T07:30:00+00:00"


def test_bare_clock_asked_evening_before_gap_finds_day_after():
    """"2:30am" asked on the evening before the spring-forward: today is past,
    tomorrow is the gap — the three-day search lands on the day after."""
    now = datetime(2026, 3, 8, 5, 0, 0, tzinfo=UTC)  # 23:00 CST Mar 7
    assert _iso(
        resolve_preferred_timing("2:30am", now=now, tz=CHICAGO)
    ) == "2026-03-09T07:30:00+00:00"


# ── Elapsed time (added to the UTC clock) ────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "2h", "in 2h", "2 hrs", "2 hours", "after 2 hours", "2 hours later",
        "in 2 hours.", "in 120m", "2 hours from now",
    ],
)
@pytest.mark.parametrize(
    ("now", "tz"),
    [(SPRING_FORWARD, CHICAGO), (FALL_BACK, CHICAGO), (SPRING_FORWARD, PHOENIX)],
)
def test_elapsed_two_hours_is_utc_delta(raw, now, tz):
    """+2h on the clock, on both sides of a clock change — never the local
    wall-clock arithmetic that lands an hour off."""
    assert _iso(resolve_preferred_timing(raw, now=now, tz=tz)) == _iso(
        now + timedelta(hours=2)
    )


@pytest.mark.parametrize(
    ("now", "expected_delta"),
    [
        (SPRING_FORWARD, timedelta(minutes=90)),
        (FALL_BACK, timedelta(minutes=90)),
    ],
)
def test_elapsed_1h30m_is_utc_delta(now, expected_delta):
    assert _iso(
        resolve_preferred_timing("in 1h30m", now=now, tz=CHICAGO)
    ) == _iso(now + expected_delta)


def test_elapsed_an_hour():
    assert _iso(
        resolve_preferred_timing("in an hour", now=NOW, tz="UTC")
    ) == _iso(NOW + timedelta(hours=1))


def test_elapsed_decimal_hours():
    assert _iso(
        resolve_preferred_timing("1.5 hours", now=NOW, tz="UTC")
    ) == _iso(NOW + timedelta(hours=1.5))


@pytest.mark.parametrize(
    "bad",
    ["in 5", "in two hours", "in -2 hours", "1430", "in 99999999999 hours"],
)
def test_elapsed_garbage_refused(bad):
    """Bare counts, number words, negatives, ambiguous HHMM, and overflow all
    refuse — nothing guesses at them."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(bad, now=SPEC_NOW, tz=CHICAGO)


# ── Calendar phrases (anchored grammar) ─────────────────────────────────


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        (CHICAGO, "2026-03-15T14:00:00+00:00"),
        ("UTC", "2026-03-15T09:00:00+00:00"),
    ],
)
def test_tomorrow_9am(tz, expected):
    assert _iso(resolve_preferred_timing("tomorrow 9am", now=NOW, tz=tz)) == expected


def test_tomorrow_9am_across_dst_boundary():
    """'tomorrow 9am' crossing the rollback resolves in local time: 9am CST."""
    now = datetime(2026, 10, 31, 15, 0, 0, tzinfo=UTC)
    assert _iso(
        resolve_preferred_timing("tomorrow 9am", now=now, tz=CHICAGO)
    ) == "2026-11-01T15:00:00+00:00"


def test_in_2_days_keeps_local_clock_time():
    """`in 2 days` is calendar, not elapsed: it lands at the same local clock
    time two days out (CDT → +49h UTC across the spring forward)."""
    now = datetime(2026, 3, 7, 7, 30, 0, tzinfo=UTC)  # 01:30 CST Mar 7
    got = resolve_preferred_timing("in 2 days", now=now, tz=CHICAGO)
    assert _local_wall(got, CHICAGO) == "2026-03-09 01:30"


def test_next_friday_3pm():
    """A weekday phrase is calendar-shaped and supported — decided question in
    the spec; see the PR body for the reasoning."""
    got = resolve_preferred_timing("next friday 3pm", now=SPEC_NOW, tz=CHICAGO)
    assert got > SPEC_NOW
    local = got.astimezone(zoneinfo.ZoneInfo(CHICAGO))
    assert local.weekday() == 4  # Friday
    assert (local.hour, local.minute) == (15, 0)


def test_tomorrow_clock_in_dst_gap_refused():
    """'tomorrow 2:30am' names a wall time that does not exist — refused, not
    silently shifted an hour."""
    now = datetime(2026, 3, 8, 5, 0, 0, tzinfo=UTC)  # evening before, CST
    with pytest.raises(TimingParseError):
        resolve_preferred_timing("tomorrow 2:30am", now=now, tz=CHICAGO)


def test_tomorrow_clock_in_dst_fold_refused():
    """'tomorrow 1:30am' across the rollback exists twice — refused, not a
    silently picked side."""
    now = datetime(2026, 11, 1, 3, 0, 0, tzinfo=UTC)  # 22:00 CDT Oct 31
    with pytest.raises(TimingParseError):
        resolve_preferred_timing("tomorrow 1:30am", now=now, tz=CHICAGO)


def test_calendar_result_beyond_horizon_refused():
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(
            "december 25 2030", now=SPEC_NOW, tz=CHICAGO
        )


# ── The refusal contract ─────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", "soonish", "banana", "yesterday"])
def test_unparseable_raises(bad):
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(bad, now=NOW, tz="UTC")


def test_result_is_utc_iso_and_lexicographically_comparable():
    later = resolve_preferred_timing("tomorrow 9am", now=NOW, tz="UTC")
    earlier = resolve_preferred_timing("in 2 hours", now=NOW, tz="UTC")
    a, b = _iso(earlier), _iso(later)
    assert a.endswith("+00:00") and b.endswith("+00:00")
    assert a < b  # lexicographic order == chronological order for UTC ISO


def test_dangling_and_refused():
    """'in 2 hours and' is an unfinished conjunction — a queued receipt for a
    truncated duration is exactly the guess this contract refuses."""
    for bad in ("in 2 hours and", "and 2 hours", "in and 2 hours"):
        with pytest.raises(TimingParseError):
            resolve_preferred_timing(bad, now=NOW, tz="UTC")
    # The connector is still valid between two durations.
    assert resolve_preferred_timing(
        "in 2 hours and 30 minutes", now=NOW, tz="UTC"
    ) == NOW + timedelta(hours=2, minutes=30)


def test_named_zone_phrase_resolves_in_that_zone():
    """'next friday 3pm UTC' from a Chicago user must land at 15:00 UTC —
    the named zone governs; the local fold check does not second-guess it."""
    got = resolve_preferred_timing("next friday 3pm UTC", now=SPEC_NOW, tz=CHICAGO)
    assert got == datetime(2026, 6, 19, 15, 0, tzinfo=UTC)
    got = resolve_preferred_timing("friday 3pm +0530", now=SPEC_NOW, tz=CHICAGO)
    assert got == datetime(2026, 6, 12, 9, 30, tzinfo=UTC)


def test_abbreviated_zone_refused():
    """CST is four different offsets worldwide — refuse rather than guess."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing("tomorrow 9am CST", now=SPEC_NOW, tz=CHICAGO)


@pytest.mark.parametrize(
    "raw",
    ["tomorrow 9am IST", "tomorrow 9am cet", "tomorrow morning", "friday 3pm sharp", "tomorrow 9am please"],
)
def test_a_word_it_cannot_read_is_refused_not_ignored(raw):
    """A date parser drops what it cannot read: 'tomorrow 9am IST' meant 9am here."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(raw, now=SPEC_NOW, tz=CHICAGO)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("tomorrow 9am", datetime(2026, 6, 11, 14, 0, tzinfo=UTC)),
        ("tomorrow at 9am", datetime(2026, 6, 11, 14, 0, tzinfo=UTC)),
        ("friday 3pm", datetime(2026, 6, 12, 20, 0, tzinfo=UTC)),
        ("next friday 3pm", datetime(2026, 6, 19, 20, 0, tzinfo=UTC)),
        ("next friday 3pm UTC", datetime(2026, 6, 19, 15, 0, tzinfo=UTC)),
        ("june 20th 3pm", datetime(2026, 6, 20, 20, 0, tzinfo=UTC)),
    ],
)
def test_ordinary_calendar_phrases_still_resolve(raw, expected):
    assert resolve_preferred_timing(raw, now=SPEC_NOW, tz=CHICAGO) == expected


def test_and_connector_is_case_insensitive():
    assert resolve_preferred_timing(
        "in 2 hours AND 30 minutes", now=NOW, tz="UTC"
    ) == NOW + timedelta(hours=2, minutes=30)


def test_short_hour_offset_is_hours_not_minutes():
    """'+5' names +05:00, not +00:05 — the offset reads as whole hours."""
    got = resolve_preferred_timing("tomorrow 9am +5", now=SPEC_NOW, tz=CHICAGO)
    # SPEC_NOW is Wednesday 2026-06-10 15:00Z → tomorrow 09:00 at +05:00 = 04:00Z.
    assert got == datetime(2026, 6, 11, 4, 0, tzinfo=UTC)


def test_zone_offset_digits_are_not_a_clock():
    """A zone needs a clock: "+05:30" on its own must not read as 05:30."""
    with pytest.raises(TimingParseError):
        resolve_preferred_timing("tomorrow +05:30", now=SPEC_NOW, tz=CHICAGO)


# ── The grammar, from an audit of the guessing parser it replaced ──
# SPEC_NOW is Wednesday 2026-06-10 15:00Z, 10:00 in Chicago (CDT, UTC-5).
_Z = lambda month, day, hour, minute=0: datetime(2026, month, day, hour, minute, tzinfo=UTC)  # noqa: E731


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("friday 13", _Z(6, 12, 18)),
        ("friday at 9:00", _Z(6, 12, 14)),
        ("next friday at 9am", _Z(6, 19, 14)),
        ("tonight 17:30", _Z(6, 10, 22, 30)),
        ("this friday 9pm", _Z(6, 13, 2)),
        ("tomorrow 14", _Z(6, 11, 19)),
        ("tomorrow at 21", _Z(6, 12, 2)),
        ("tomorrow 9am UTC", _Z(6, 11, 9)),
        ("tomorrow 9am UTC+5", _Z(6, 11, 4)),
        ("tomorrow 9am GMT+05:30", _Z(6, 11, 3, 30)),
        ("tomorrow 9am +05:30", _Z(6, 11, 3, 30)),
        ("tomorrow 9am -5", _Z(6, 11, 14)),
        ("9am UTC", _Z(6, 11, 9)),
        ("june 20 3pm", _Z(6, 20, 20)),
        ("june 20, 2026 3pm", _Z(6, 20, 20)),
        ("20 june 3pm", _Z(6, 20, 20)),
        ("20th of june 3pm", _Z(6, 20, 20)),
        ("jun 20 at 3pm", _Z(6, 20, 20)),
        ("wednesday 3pm", _Z(6, 10, 20)),
        ("wednesday 9am", _Z(6, 17, 14)),
        ("next wednesday 3pm", _Z(6, 17, 20)),
        ("next monday 3pm", _Z(6, 15, 20)),
        ("this friday 3pm", _Z(6, 12, 20)),
        ("TOMORROW 9AM", _Z(6, 11, 14)),
        ("on friday at 3pm", _Z(6, 12, 20)),
        ("tomorrow noon", _Z(6, 11, 17)),
        ("noon tomorrow", _Z(6, 11, 17)),
        ("friday noon", _Z(6, 12, 17)),
        ("3pm tomorrow", _Z(6, 11, 20)),
        ("9am on friday", _Z(6, 12, 14)),
        ("tonight 9pm", _Z(6, 11, 2)),
        ("tonight at 9", _Z(6, 11, 2)),
        ("today 3pm", _Z(6, 10, 20)),
        ("tomorrow", _Z(6, 11, 15)),
        ("day after tomorrow 9am", _Z(6, 12, 14)),
        ("2 days from now", _Z(6, 12, 15)),
        ("a week from now", _Z(6, 17, 15)),
        ("2 days later", _Z(6, 12, 15)),
        ("in a week", _Z(6, 17, 15)),
        ("in 1 week", _Z(6, 17, 15)),
    ],
)
def test_the_grammar_resolves(raw, expected):
    assert resolve_preferred_timing(raw, now=SPEC_NOW, tz=CHICAGO) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "friday at 25", "3 friday", "9 UTC", "friday 9-5", "friday", "next friday",
        "on friday", "may", "sept", "in june", "june 2027", "may 5", "june 20th",
        "tomorrow 99", "tomorrow 1430", "tomorrow 9am1", "tomorrow 9:5am", "tomorrow 14:30:45",
        "june-20 3pm", "june-20-2026 3pm", "june 5-6 3pm", "may 5-10 3pm", "june 31 3pm",
        "june 20 -2026", "monday june 20 3pm", "friday the 13th", "tomorrow 9am 5pm",
        "tomorrow 9am-5pm", "today 3pm tomorrow 4pm", "today tomorrow", "tomorrow tomorrow",
        "the tomorrow", "tomorrow a", "tomorrow 9am +25", "tomorrow 9am +24", "tomorrow 9am -24:00",
        "tomorrow 9am +05:99", "tomorrow 9am +5:3", "tomorrow 9am UTC PST", "tomorrow 9am +5 UTC",
        "tomorrow 9am +5 -3", "tomorrow 9am IST", "tomorrow 9am Z", "tomorrow 9am \u00e0",
        "am", "in am", "am from now", "as", "ah", "in 0 days", "0h", "in 0 hours", "in -1 days",
        "in 999999 hours", "8760000h", "in 400 days", "14300101", "today 9am", "tonight 9am",
        "tonight", "today", "yesterday", "by friday", "tomorrow morning",
        "january 1 2020 3pm", "june 20 2028 3pm",
        "friday at 9", "friday 9", "next friday at 9", "tomorrow at 9", "june 20 3",
        "tonight midnight", "midnight tonight", "tonight at 0", "tonight 00:00", "tonight 0:30",
        "tonight at 12", "tonight 12:30", "tonight at noon", "tonight 12pm", "tonight at 1:00",
        "tonight 14:00", "tonight 3pm", "tomorrow midnight", "wednesday midnight",
        "friday midnight", "midnight today", "this wednesday 9am", "this monday 8am",
        "tomorrow -5", "friday +5", "2026-11-05", "2026-W45-4", "a m", "an h", "a s", "1h 1h",
        "\u017fat 9am", "fr\u0131day 9am", "\u017fep 3 9am", "june 1\u017ft 9am",
        "\u0669am", "\uff19:\uff13\uff10pm", "tomorrow 9am utc+\u0665",
    ],
)
def test_the_grammar_refuses(raw):
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(raw, now=SPEC_NOW, tz=CHICAGO)


@pytest.mark.parametrize(
    ("raw", "now", "expected"),
    [
        # A calendar day keeps the local clock across a clock change.
        ("tomorrow", datetime(2026, 3, 7, 16, 0, tzinfo=UTC), datetime(2026, 3, 8, 15, 0, tzinfo=UTC)),
        ("in a week", datetime(2026, 3, 7, 16, 0, tzinfo=UTC), datetime(2026, 3, 14, 15, 0, tzinfo=UTC)),
        ("in a week", datetime(2026, 10, 31, 15, 0, tzinfo=UTC), datetime(2026, 11, 7, 16, 0, tzinfo=UTC)),
    ],
)
def test_a_calendar_day_keeps_the_clock_across_dst(raw, now, expected):
    assert resolve_preferred_timing(raw, now=now, tz=CHICAGO) == expected


@pytest.mark.parametrize(
    ("raw", "now"),
    [
        ("tomorrow", datetime(2026, 3, 7, 8, 30, tzinfo=UTC)),  # 02:30 is in the gap
        ("in a day", datetime(2026, 3, 7, 8, 30, tzinfo=UTC)),
        ("tomorrow 2:30am", datetime(2026, 3, 7, 16, 0, tzinfo=UTC)),
        ("tomorrow 1:30am", datetime(2026, 10, 31, 15, 0, tzinfo=UTC)),  # exists twice
    ],
)
def test_a_wall_time_in_a_gap_or_fold_is_refused(raw, now):
    with pytest.raises(TimingParseError):
        resolve_preferred_timing(raw, now=now, tz=CHICAGO)


def test_a_zone_name_that_is_a_directory_is_refused():
    with pytest.raises(TimingParseError):
        resolve_preferred_timing("9am", now=SPEC_NOW, tz="America")


@pytest.mark.parametrize(
    "raw", ["1" * 65, "1" * 100_000, "tomorrow" + " " * 100_000 + "x", "in " + "9" * 5000 + " days"]
)
def test_a_long_string_is_refused_before_any_regex_runs(raw):
    with pytest.raises(TimingParseError, match="longer than"):
        resolve_preferred_timing(raw, now=SPEC_NOW, tz=CHICAGO)


def test_tonight_with_a_bare_hour_is_the_evening():
    assert resolve_preferred_timing("tonight at 9", now=SPEC_NOW, tz=CHICAGO) == _Z(6, 11, 2)
