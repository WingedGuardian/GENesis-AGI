"""Tests for genesis.util.tz — timezone display helpers."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import genesis.util.tz as tz_module
from genesis.util.tz import fmt, fmt_short, local_day_boundary, parse_utc_iso


@pytest.fixture()
def london_tz(monkeypatch):
    """Pin _USER_TZ to a DST-observing zone (Europe/London: GMT/BST) for the
    display-format tests, which need a real named zone to exercise the seasonal
    abbreviation. Deliberately NOT the install's own timezone — install-generic,
    so no private location identifier lands in the public repo.
    """
    monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("Europe/London"))


class TestFmt:
    def test_utc_to_standard_time(self, london_tz):
        # January = GMT (UTC+0): 17:00 UTC = 17:00 GMT
        result = fmt("2026-01-15T17:00:00+00:00")
        assert "17:00" in result
        assert "GMT" in result

    def test_utc_to_dst(self, london_tz):
        # July = BST (UTC+1): 17:00 UTC = 18:00 BST
        result = fmt("2026-07-15T17:00:00+00:00")
        assert "18:00" in result
        assert "BST" in result

    def test_naive_assumed_utc(self, london_tz):
        result = fmt("2026-01-15T17:00:00")
        assert "17:00" in result
        assert "GMT" in result

    def test_invalid_string_returns_original(self):
        assert fmt("not-a-date") == "not-a-date"

    def test_none_returns_unknown(self):
        assert fmt(None) == "unknown"  # type: ignore[arg-type]

    def test_empty_string_returns_unknown(self):
        assert fmt("") == "unknown"

    def test_custom_format(self, london_tz):
        result = fmt("2026-01-15T17:00:00+00:00", "%H:%M %Z")
        assert result == "17:00 GMT"


class TestFmtShort:
    def test_short_format(self, london_tz):
        result = fmt_short("2026-01-15T17:00:00+00:00")
        assert result == "17:00 GMT"


class TestParseUtcIso:
    def test_aware_string_preserved(self):
        dt = parse_utc_iso("2026-06-20T17:00:00+00:00")
        assert dt == datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
        assert dt.tzinfo is not None

    def test_naive_string_assumed_utc(self):
        dt = parse_utc_iso("2026-06-20T17:00:00")
        assert dt == datetime(2026, 6, 20, 17, 0, tzinfo=UTC)
        assert dt.tzinfo is UTC

    def test_naive_result_is_aware_and_subtractable(self):
        # The core bug this guards against: naive value subtracted from aware now.
        dt = parse_utc_iso("2026-06-20T17:00:00")
        delta = datetime.now(UTC) - dt  # must not raise
        assert delta.total_seconds() >= 0

    def test_none_returns_none(self):
        assert parse_utc_iso(None) is None

    def test_empty_returns_none(self):
        assert parse_utc_iso("") is None

    def test_invalid_returns_none(self):
        assert parse_utc_iso("not-a-timestamp") is None


@pytest.fixture()
def utc_minus_4(monkeypatch):
    """Pin _USER_TZ to a fixed UTC-4 offset (Etc/GMT+4) — no real location, no DST."""
    monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("Etc/GMT+4"))  # Etc/GMT+4 == UTC-4


class TestLocalDayBoundary:
    """The day boundary must be LOCAL midnight (hour), not UTC midnight.

    Regression guard for the CC-session daily-reset bug: sessions reset at the
    UTC-midnight instant instead of local midnight because the boundary was
    computed in UTC. The boundary must also be returned UTC-aware so a string
    ``<`` comparison against stored ``+00:00`` timestamps stays lexicographically
    correct (the query_stale trap). Fixed offsets (Etc/GMT+N) keep these tests
    install-generic — no real timezone is hardcoded.
    """

    def test_boundary_is_local_midnight_not_utc(self, utc_minus_4):
        # now = 2026-08-24 03:00 UTC = 2026-08-23 23:00 at UTC-4 (still Aug 23 local).
        # Most recent LOCAL midnight = Aug 23 00:00 (UTC-4) = 2026-08-23 04:00 UTC.
        # The buggy UTC-midnight logic would give 2026-08-24 00:00 UTC.
        now = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
        assert local_day_boundary(0, now=now) == datetime(2026, 8, 23, 4, 0, tzinfo=UTC)

    def test_boundary_returned_utc_aware_plus_zero_offset(self, utc_minus_4):
        # The ISO-string trap: boundary must serialize with +00:00, never a
        # local offset, or the query_stale string comparison breaks.
        now = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
        boundary = local_day_boundary(0, now=now)
        assert boundary.utcoffset() == timedelta(0)
        assert boundary.isoformat().endswith("+00:00")

    def test_boundary_respects_zone_offset(self, monkeypatch):
        # A different fixed offset (UTC-5) yields a different UTC boundary,
        # proving the boundary follows the zone, not a hardcoded offset.
        monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("Etc/GMT+5"))  # UTC-5
        # now = 2026-01-15 04:00 UTC = 2026-01-14 23:00 at UTC-5.
        # Local midnight Jan 14 00:00 (UTC-5) = 2026-01-14 05:00 UTC.
        now = datetime(2026, 1, 15, 4, 0, tzinfo=UTC)
        assert local_day_boundary(0, now=now) == datetime(2026, 1, 14, 5, 0, tzinfo=UTC)

    def test_boundary_just_after_local_midnight_is_today(self, utc_minus_4):
        # now = 2026-08-24 05:00 UTC = 2026-08-24 01:00 at UTC-4 (just past local midnight).
        # Boundary = Aug 24 00:00 (UTC-4) = 2026-08-24 04:00 UTC (today's, no rollback).
        now = datetime(2026, 8, 24, 5, 0, tzinfo=UTC)
        assert local_day_boundary(0, now=now) == datetime(2026, 8, 24, 4, 0, tzinfo=UTC)

    def test_boundary_nonzero_hour(self, utc_minus_4):
        # hour=6 (06:00 local). now = 2026-08-24 03:00 UTC = 2026-08-23 23:00 at UTC-4.
        # Most recent 06:00 (UTC-4) = Aug 23 06:00 = 2026-08-23 10:00 UTC.
        now = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
        assert local_day_boundary(6, now=now) == datetime(2026, 8, 23, 10, 0, tzinfo=UTC)

    def test_utc_install_unaffected(self, monkeypatch):
        # A UTC install: boundary IS UTC midnight (no behavior change).
        monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("UTC"))
        now = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
        assert local_day_boundary(0, now=now) == datetime(2026, 8, 24, 0, 0, tzinfo=UTC)

    def test_default_now_returns_recent_past_utc(self, utc_minus_4):
        # Smoke test with real now: boundary is UTC-aware and in the past.
        boundary = local_day_boundary(0)
        assert boundary.tzinfo is not None
        assert boundary <= datetime.now(UTC)

    def test_naive_now_treated_as_utc(self, utc_minus_4):
        # A naive `now` is interpreted as UTC (not system-local), matching the
        # module convention. 03:00 naive == 03:00 UTC == 23:00 prev day at UTC-4.
        naive = datetime(2026, 8, 24, 3, 0)  # noqa: DTZ001 - deliberately naive
        aware = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
        assert local_day_boundary(0, now=naive) == local_day_boundary(0, now=aware)
        assert local_day_boundary(0, now=naive) == datetime(2026, 8, 23, 4, 0, tzinfo=UTC)

    def test_boundary_monotonic_across_midnight_dst_fallback(self, monkeypatch):
        # A zone that falls back at LOCAL midnight (America/Havana on 2026-11-01)
        # makes midnight ambiguous — it occurs twice. The boundary must pin to a
        # single fold so it never jumps backward across the repeated hour;
        # otherwise the daily reset can fire twice on one local date.
        monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("America/Havana"))
        boundaries = [
            local_day_boundary(0, now=datetime(2026, 11, 1, h, 0, tzinfo=UTC)) for h in range(2, 10)
        ]
        assert boundaries == sorted(boundaries)  # monotonic non-decreasing
        # During the repeated midnight (05:00Z) the boundary stays at the FIRST
        # occurrence (04:00Z), not the second — the fold=0 pin.
        assert local_day_boundary(0, now=datetime(2026, 11, 1, 5, 0, tzinfo=UTC)) == datetime(
            2026, 11, 1, 4, 0, tzinfo=UTC
        )

    def test_boundary_never_future_across_spring_forward_gap(self, monkeypatch):
        # A multi-hour spring-forward jump can map a nonexistent boundary wall
        # time to a FUTURE UTC instant when compared in local wall-clock. The
        # boundary must never be later than now — enforced by the UTC comparison
        # + windowed candidate selection (else both reset consumers treat fresh
        # sessions as stale until that future instant). Antarctica/Casey jumped 3h
        # on 2020-10-04; at 03:15 local (16:15Z) the 03:00 boundary is in the gap.
        monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("Antarctica/Casey"))
        now = datetime(2020, 10, 3, 16, 15, tzinfo=UTC)
        boundary = local_day_boundary(3, now=now)
        assert boundary <= now  # never in the future
        assert boundary == datetime(2020, 10, 2, 19, 0, tzinfo=UTC)

    def test_boundary_never_future_across_fallback_over_midnight(self, monkeypatch):
        # THE 4th DST edge (windowed-max regression guard). A >1h fall-back moves
        # the wall clock BACKWARD across the boundary hour, regressing the local
        # date. The most recent real boundary then sits on a local date one day
        # AHEAD of now's local date yet already elapsed in UTC — a loop that only
        # walks BACKWARD from now's local date can never generate it and returns a
        # 24h-stale, non-monotonic boundary. Antarctica/Casey fell back +11→+08 at
        # 2010-03-04 15:00Z (wall clock Mar-05 02:00 → Mar-04 23:00).
        monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("Antarctica/Casey"))
        now = datetime(2010, 3, 4, 15, 0, tzinfo=UTC)  # 23:00 Mar-04 local (regressed)
        boundary = local_day_boundary(0, now=now)
        assert boundary <= now  # never in the future
        # Correct = local Mar-05 00:00 fold=0 = 2010-03-04T13:00Z (already elapsed),
        # NOT local Mar-04 00:00 = 2010-03-03T13:00Z (24h stale).
        assert boundary == datetime(2010, 3, 4, 13, 0, tzinfo=UTC)

    def test_boundary_monotonic_across_fallback_over_midnight(self, monkeypatch):
        # The same fall-back must not make the boundary jump BACKWARD as now
        # advances (the round-1 non-monotonicity class, re-guarded for this edge).
        monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("Antarctica/Casey"))
        boundaries = [
            local_day_boundary(0, now=datetime(2010, 3, 4, h, 0, tzinfo=UTC)) for h in range(12, 20)
        ]
        assert boundaries == sorted(boundaries)  # monotonic non-decreasing

    def test_boundary_across_skipped_whole_calendar_day(self, monkeypatch):
        # Pacific/Apia crossed the dateline and DELETED 2011-12-30 entirely. The
        # most recent real boundary is TWO local dates back — a single fixed day
        # step-back is insufficient (imaginary Dec-30 06:00 normalizes to the same
        # future instant as Dec-31 06:00). Must resolve to the most recent REAL
        # boundary <= now (Dec 29 06:00 local = 2011-12-29T16:00Z).
        monkeypatch.setattr(tz_module, "_USER_TZ", ZoneInfo("Pacific/Apia"))
        now = datetime(2011, 12, 30, 10, 30, tzinfo=UTC)  # 00:30 Dec 31 local
        boundary = local_day_boundary(6, now=now)
        assert boundary <= now  # never in the future
        assert boundary == datetime(2011, 12, 29, 16, 0, tzinfo=UTC)

    def test_boundary_brute_force_matches_wide_oracle(self, monkeypatch):
        # Correct-by-construction PROOF (not sampled point-patches): across a
        # curated set of pathological zones x representative hours x a
        # transition-dense now-grid, local_day_boundary must (a) equal an
        # independent WIDE-window oracle, (b) never be in the future, and (c) be
        # monotonic non-decreasing as now advances. Checking the whole
        # (zone, hour, now) grid — not a handful of shapes — is what closes the
        # DST class so a new edge cannot slip through. Verify-RED: this fails
        # against the prior single-step-back loop (Apia 2011 skip + Casey 2010
        # fall-back-over-midnight both land in the window).
        zones = [
            "Antarctica/Casey",
            "Pacific/Apia",
            "America/Havana",
            "Australia/Lord_Howe",
            "Pacific/Chatham",
            "Pacific/Kiritimati",
            "America/Sao_Paulo",
            "Etc/GMT+5",
        ]
        hours = [0, 3, 6, 23]

        def wide_oracle(hour: int, now_utc: datetime, zone: ZoneInfo) -> datetime:
            # Reference: window STRICTLY WIDER (-2..4) than the function's own
            # (-1..2), so agreement proves the narrow window sufficient and the
            # max-<=-now selection correct. (Wider than needed: max real DST
            # date-shift is one day either way, so -2..4 already over-covers.)
            nl = now_utc.astimezone(zone)
            cands = [
                (nl - timedelta(days=d))
                .replace(hour=hour, minute=0, second=0, microsecond=0, fold=0)
                .astimezone(UTC)
                for d in range(-2, 5)
            ]
            past = [c for c in cands if c <= now_utc]
            return max(past) if past else min(cands)

        start = datetime(2010, 1, 1, tzinfo=UTC)
        step = timedelta(hours=12)
        n_steps = int(timedelta(days=730) / step)  # ~2 years, transition-dense
        for zname in zones:
            zone = ZoneInfo(zname)
            monkeypatch.setattr(tz_module, "_USER_TZ", zone)
            for hour in hours:
                prev = None
                now = start
                for _ in range(n_steps):
                    got = local_day_boundary(hour, now=now)
                    assert got <= now, (
                        f"{zname} h={hour} now={now.isoformat()} future={got.isoformat()}"
                    )
                    assert got == wide_oracle(hour, now, zone), (
                        f"{zname} h={hour} now={now.isoformat()} "
                        f"got={got.isoformat()} oracle={wide_oracle(hour, now, zone).isoformat()}"
                    )
                    if prev is not None:
                        assert got >= prev, (
                            f"{zname} h={hour} backward jump {prev.isoformat()} -> "
                            f"{got.isoformat()} at now={now.isoformat()}"
                        )
                    prev = got
                    now += step
