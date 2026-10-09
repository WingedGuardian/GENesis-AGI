"""The FalkorDB default-on cutover verdict (A1b-2).

One test per rule the verdict applies, built from synthetic rows in the exact
shapes the writers produce (``graph_telemetry`` and ``graph_census``). The rules
are owner decisions; every boundary is pinned on both sides.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from genesis.memory import graph_cutover as cut

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _proc(**over) -> dict:
    p = {
        "pid": 1,
        "started_at": "x",
        "commit": "c",
        "held": ["c"],
        "telemetry": True,
        "unknown": None,
        "telemetry_off": False,
        "foreign": False,
    }
    p.update(over)
    return p


def _census_metrics(**over) -> dict:
    m = {
        "v": 1,
        "procs": [_proc()],
        "other_servers": {},
        "unclassified": 0,
        "head_telemetry": True,
        "head_dirty": False,
        "complete": True,
        "reasons": [],
    }
    m.update(over)
    return m


def _census(start: datetime, end: datetime, step=timedelta(hours=1), **over):
    rows, t = [], start
    while t <= end:
        rows.append((_ts(t), json.dumps(_census_metrics(**over))))
        t += step
    return rows


def _trav(t: datetime, *, primary=1, cancelled=0, **over) -> tuple[str, str]:
    outcomes = {k: v for k, v in (("primary", primary), ("cancelled", cancelled)) if v}
    served = {k: v for k, v in (("falkordb", primary), ("none", cancelled)) if v}
    m = {
        "caller": "recall",
        "proc": "mcp-memory",
        "traversals": primary + cancelled,
        "outcomes": outcomes,
        "served": served,
        "configured": {"falkordb": primary + cancelled},
        "events": [],
        "prior_write_failures": 0,
    }
    m.update(over)
    return (_ts(t), json.dumps(m))


def _daily_traffic(days: int, per_day: int = 12) -> list:
    return [_trav(NOW - timedelta(days=d, hours=1), primary=per_day) for d in range(days)]


def _passing_inputs(clock_days: float = 15):
    census = _census(NOW - timedelta(days=clock_days), NOW)
    return _daily_traffic(14), census


# ── PASS and its boundaries ───────────────────────────────────────────────


def test_fourteen_clean_days_with_enough_traffic_pass():
    trav, census = _passing_inputs()
    v = cut.evaluate(trav, census, [], NOW)
    assert v.status == "PASS", v.reasons
    assert v.falkordb_traversals == 14 * 12
    assert v.days_with_traffic == 14


def test_exactly_fourteen_days_on_the_clock_is_enough_and_a_minute_less_is_not():
    trav = _daily_traffic(14)
    exact = _census(NOW - timedelta(days=14), NOW)
    assert cut.evaluate(trav, exact, [], NOW).status == "PASS"
    short = _census(NOW - timedelta(days=14) + timedelta(minutes=1), NOW)
    v = cut.evaluate(trav, short, [], NOW)
    assert v.status == "NOT_YET"
    assert any("of 14 days on the clock" in r for r in v.reasons)


@pytest.mark.parametrize(("per_day", "status"), [(10, "PASS"), (9, "NOT_YET")])
def test_at_least_one_hundred_falkordb_traversals(per_day, status):
    # 10 days of traffic: 100 passes, 90 does not.
    trav = _daily_traffic(10, per_day=per_day)
    v = cut.evaluate(trav, _census(NOW - timedelta(days=15), NOW), [], NOW)
    assert v.status == status


@pytest.mark.parametrize(("days", "status"), [(10, "PASS"), (9, "NOT_YET")])
def test_traffic_on_at_least_ten_distinct_days(days, status):
    trav = _daily_traffic(days, per_day=20)
    v = cut.evaluate(trav, _census(NOW - timedelta(days=15), NOW), [], NOW)
    assert v.status == status


def test_traffic_older_than_fourteen_days_does_not_count():
    old = [_trav(NOW - timedelta(days=15), primary=500)]
    v = cut.evaluate(old, _census(NOW - timedelta(days=20), NOW), [], NOW)
    assert v.falkordb_traversals == 0
    assert v.status == "NOT_YET"


# ── traversal rows that restart the clock (an allowlist) ──────────────────


@pytest.mark.parametrize(
    "outcome", ["fallback", "selection_failed", "error", "mode_unsupported", "brand_new"]
)
def test_every_outcome_but_primary_and_cancelled_restarts_the_clock(outcome):
    trav, census = _passing_inputs(clock_days=20)
    bad_at = NOW - timedelta(days=3)
    trav.append(
        (
            _ts(bad_at),
            json.dumps(
                {
                    "caller": "recall",
                    "proc": "mcp-memory",
                    "traversals": 1,
                    "outcomes": {outcome: 1},
                    # Served by falkordb, so the OUTCOME is the only thing that can
                    # restart the clock here: an unknown name must, by allowlist.
                    "served": {"falkordb": 1},
                    "configured": {"falkordb": 1},
                    "events": [{"outcome": outcome}],
                    "prior_write_failures": 0,
                }
            ),
        )
    )
    v = cut.evaluate(trav, census, [], NOW)
    assert v.status == "NOT_YET"
    assert v.clock_start == bad_at
    assert v.resets[-1]["proc"] == "mcp-memory"


def test_a_networkx_configuration_restarts_the_clock():
    trav, census = _passing_inputs(clock_days=20)
    trav.append(_trav(NOW - timedelta(days=2), configured={"networkx": 1}))
    assert cut.evaluate(trav, census, [], NOW).status == "NOT_YET"


def test_serving_by_another_store_restarts_the_clock_even_as_primary():
    trav, census = _passing_inputs(clock_days=20)
    trav.append(_trav(NOW - timedelta(days=2), served={"networkx": 1}))
    assert cut.evaluate(trav, census, [], NOW).status == "NOT_YET"


def test_a_cancellation_is_neutral_but_counted():
    trav, census = _passing_inputs()
    trav.append(_trav(NOW - timedelta(hours=3), primary=0, cancelled=4))
    v = cut.evaluate(trav, census, [], NOW)
    assert v.status == "PASS"
    assert v.cancelled == 4
    assert v.traversals == 14 * 12 + 4


def test_an_old_reset_before_the_window_does_not_hold_the_clock_back():
    trav, census = _passing_inputs(clock_days=40)
    trav.append(_trav(NOW - timedelta(days=30), served={"networkx": 1}))
    v = cut.evaluate(trav, census, [], NOW)
    assert v.status == "PASS"
    assert v.clock_start == NOW - timedelta(days=30)


# ── evidence that may be missing: INCONCLUSIVE ────────────────────────────


def test_a_row_carrying_lost_writes_inside_the_window_is_inconclusive():
    trav, census = _passing_inputs()
    trav.append(_trav(NOW - timedelta(days=1), prior_write_failures=2))
    v = cut.evaluate(trav, census, [], NOW)
    assert v.status == "INCONCLUSIVE"
    assert any("2 telemetry row(s) lost" in r for r in v.reasons)


def test_lost_writes_that_aged_out_of_the_window_no_longer_count():
    trav, census = _passing_inputs(clock_days=20)
    trav.append(_trav(NOW - timedelta(days=15), prior_write_failures=2))
    assert cut.evaluate(trav, census, [], NOW).status == "PASS"


def test_a_lost_writes_file_line_inside_the_window_is_inconclusive():
    trav, census = _passing_inputs()
    line = json.dumps({"ts": _ts(NOW - timedelta(days=2)), "caller": "recall"})
    old = json.dumps({"ts": _ts(NOW - timedelta(days=20)), "caller": "recall"})
    assert cut.evaluate(trav, census, [old], NOW).status == "PASS"
    assert cut.evaluate(trav, census, [old, line], NOW).status == "INCONCLUSIVE"


def test_an_unreadable_lost_writes_line_is_inconclusive():
    trav, census = _passing_inputs()
    assert cut.evaluate(trav, census, ["not json"], NOW).status == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "metrics",
    [
        "not json",
        json.dumps({"traversals": 1}),
        json.dumps(
            {
                "traversals": 3,
                "outcomes": {"primary": 1},
                "served": {"falkordb": 1},
                "configured": {"falkordb": 1},
            }
        ),
    ],
)
def test_an_unreadable_traversal_row_in_the_window_is_inconclusive(metrics):
    trav, census = _passing_inputs()
    trav.append((_ts(NOW - timedelta(days=1)), metrics))
    assert cut.evaluate(trav, census, [], NOW).status == "INCONCLUSIVE"


def test_no_census_at_all_is_inconclusive():
    v = cut.evaluate(_daily_traffic(14), [], [], NOW)
    assert v.status == "INCONCLUSIVE"
    assert v.clock_start is None


def test_a_stale_census_is_inconclusive():
    census = _census(NOW - timedelta(days=20), NOW - timedelta(hours=3))
    assert cut.evaluate(_daily_traffic(14), census, [], NOW).status == "INCONCLUSIVE"


def test_telemetry_switched_off_in_the_server_is_inconclusive():
    census = _census(NOW - timedelta(days=20), NOW - timedelta(hours=2))
    census.append(
        (
            _ts(NOW - timedelta(minutes=10)),
            json.dumps({"v": 1, "procs": [], "complete": False, "reasons": ["telemetry_disabled"]}),
        )
    )
    v = cut.evaluate(_daily_traffic(14), census, [], NOW)
    assert v.status == "INCONCLUSIVE"
    assert "telemetry was switched off inside the window" in v.reasons


# ── the census clock ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("over", "why"),
    [
        ({"procs": [_proc(telemetry=False)]}, "may run old code"),
        ({"procs": [_proc(telemetry=None, unknown="the reflog has a gap")]}, "reflog has a gap"),
        ({"procs": [_proc(telemetry_off=True)]}, "kill switch on"),
        ({"complete": False, "reasons": ["proc_unreadable"]}, "incomplete census"),
        ({"unclassified": 1}, "unknown kind"),
        ({"head_telemetry": False}, "HEAD does not contain"),
        ({"head_dirty": True}, "dirty"),
        ({"v": 99}, "unknown census schema"),
    ],
)
def test_a_dirty_census_row_holds_the_clock(over, why):
    census = _census(NOW - timedelta(days=20), NOW - timedelta(days=5))
    census += _census(NOW - timedelta(days=5) + timedelta(hours=1), NOW, **over)
    v = cut.evaluate(_daily_traffic(14), census, [], NOW)
    assert v.status == "NOT_YET"
    assert v.clock_start is None
    assert any(why in r for r in v.reasons), v.reasons


def test_a_foreign_bench_server_does_not_hold_the_clock():
    census = _census(
        NOW - timedelta(days=15), NOW, procs=[_proc(), _proc(pid=2, telemetry=False, foreign=True)]
    )
    assert cut.evaluate(_daily_traffic(14), census, [], NOW).status == "PASS"


def test_the_clock_starts_at_the_first_clean_census_after_an_old_server_exits():
    dirty_until = NOW - timedelta(days=16)
    census = _census(
        NOW - timedelta(days=20), dirty_until, procs=[_proc(), _proc(pid=2, telemetry=False)]
    )
    census += _census(dirty_until + timedelta(hours=1), NOW)
    v = cut.evaluate(_daily_traffic(14), census, [], NOW)
    assert v.status == "PASS"
    assert v.clock_start == dirty_until + timedelta(hours=1)


@pytest.mark.parametrize(
    ("gap", "restarts"),
    [(timedelta(hours=2, minutes=15), False), (timedelta(hours=2, minutes=16), True)],
)
def test_one_missed_census_slot_is_tolerated_and_more_restarts_the_clock(gap, restarts):
    first_end = NOW - timedelta(days=10)
    census = _census(NOW - timedelta(days=20), first_end)
    census += _census(first_end + gap, NOW)
    v = cut.evaluate(_daily_traffic(14), census, [], NOW)
    if restarts:
        assert v.status == "NOT_YET"
        assert v.clock_start == first_end + gap
    else:
        assert v.status == "PASS"


def test_the_json_view_carries_every_field_the_report_prints():
    trav, census = _passing_inputs()
    data = cut.evaluate(trav, census, [], NOW).to_json()
    assert data["status"] == "PASS"
    assert data["days_on_clock"] == 15.0
    assert set(data) >= {
        "clock_start",
        "reasons",
        "resets",
        "falkordb_traversals",
        "days_with_traffic",
        "cancelled",
        "traversals",
        "census_rows",
        "last_census",
    }


# ── review fixes ──────────────────────────────────────────────────────────


def test_a_replay_ignores_everything_after_its_now():
    """Evaluating as of an earlier moment must not see traffic or census rows
    that had not happened yet."""
    trav, census = _passing_inputs(clock_days=40)
    then = NOW - timedelta(days=13)
    v = cut.evaluate(trav, census, [], then)
    assert v.status == "NOT_YET"
    assert v.falkordb_traversals == 12  # only the day at `then - 1h`
    assert v.last_census <= then


def test_a_census_row_after_now_cannot_hide_a_stale_census():
    census = _census(NOW - timedelta(days=20), NOW - timedelta(hours=5))
    census.append((_ts(NOW + timedelta(hours=1)), json.dumps(_census_metrics())))
    assert cut.evaluate(_daily_traffic(14), census, [], NOW).status == "INCONCLUSIVE"


@pytest.mark.parametrize(
    "over",
    [
        {"served": {"falkordb": 50}},
        {"configured": {}},
        {"outcomes": {"primary": "1"}},
        {"prior_write_failures": "2"},
    ],
)
def test_counts_that_do_not_add_up_or_are_not_integers_are_unreadable(over):
    trav, census = _passing_inputs()
    trav.append(_trav(NOW - timedelta(days=1), **over))
    assert cut.evaluate(trav, census, [], NOW).status == "INCONCLUSIVE"


def test_telemetry_switched_off_earlier_in_the_window_is_inconclusive():
    census = _census(NOW - timedelta(days=20), NOW - timedelta(days=5))
    census.append(
        (
            _ts(NOW - timedelta(days=5) + timedelta(minutes=30)),
            json.dumps({"v": 1, "procs": [], "complete": False, "reasons": ["telemetry_disabled"]}),
        )
    )
    census += _census(NOW - timedelta(days=5) + timedelta(hours=1), NOW)
    v = cut.evaluate(_daily_traffic(14), census, [], NOW)
    assert v.status == "INCONCLUSIVE"


def test_a_clean_and_a_dirty_row_at_the_same_moment_count_as_dirty():
    at = NOW - timedelta(days=15)
    dirty = json.dumps(_census_metrics(head_dirty=True))
    clean = json.dumps(_census_metrics())
    # Clean listed after dirty: a naive stable sort would end on the clean row.
    census = [(_ts(at), dirty), (_ts(at), clean)] + _census(at + timedelta(hours=1), NOW)
    v = cut.evaluate(_daily_traffic(14), census, [], NOW)
    assert v.clock_start == at + timedelta(hours=1)


def test_resets_before_the_window_are_labelled():
    trav, census = _passing_inputs(clock_days=40)
    trav.append(_trav(NOW - timedelta(days=30), served={"networkx": 1}))
    [old] = cut.evaluate(trav, census, [], NOW).resets
    assert old["in_window"] is False

