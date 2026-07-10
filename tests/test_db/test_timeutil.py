"""canonical_iso — the single write-path gate for bitemporal timestamps.

Format matrix mirrors the live-DB census (2026-07-09): canonical iso_tz,
Z-suffix (2,632 rows), naive-T (67), space-separated (55+5), non-UTC
offsets (11), date-only (5,488 — canonical, untouched), plus the
unparseable LLM temporal strings ("Friday", ranges, month-only).
"""

from __future__ import annotations

import pytest

from genesis.db.timeutil import canonical_iso


def test_none_and_empty():
    assert canonical_iso(None) is None
    assert canonical_iso("") is None
    assert canonical_iso("   ") is None


def test_canonical_passthrough():
    assert (
        canonical_iso("2026-07-09T12:00:00+00:00") == "2026-07-09T12:00:00+00:00"
    )


def test_canonical_microseconds_preserved():
    ts = "2026-07-09T12:00:00.123456+00:00"
    assert canonical_iso(ts) == ts


def test_z_suffix_becomes_utc_offset():
    assert canonical_iso("2026-05-03T17:30:26Z") == "2026-05-03T17:30:26+00:00"


def test_naive_t_assumed_utc():
    assert canonical_iso("2026-05-03T17:30:26") == "2026-05-03T17:30:26+00:00"


def test_naive_minute_precision_padded():
    assert canonical_iso("2026-05-20T22:41") == "2026-05-20T22:41:00+00:00"


def test_space_separator_canonicalized():
    assert canonical_iso("2026-04-03 13:30:54") == "2026-04-03T13:30:54+00:00"


def test_non_utc_offset_converted_to_utc():
    assert (
        canonical_iso("2026-05-11T17:00:00-04:00") == "2026-05-11T21:00:00+00:00"
    )


def test_date_only_is_canonical_untouched():
    assert canonical_iso("2026-06-14") == "2026-06-14"


@pytest.mark.parametrize(
    "garbage",
    [
        "Friday",
        "July 3",
        "commit dfce0319 (PR #58)",
        "2026-04",
        "2026-03-18/2026-03-28",
        "2026-05-13 to 2026-05-18",
    ],
)
def test_unparseable_returns_none(garbage):
    assert canonical_iso(garbage) is None


def test_sort_order_contract():
    """The point of it all: canonical strings TEXT-compare correctly."""
    earlier = canonical_iso("2026-05-03T17:30:26Z")
    later = canonical_iso("2026-05-03 17:30:27")
    assert earlier is not None and later is not None
    assert earlier < later
