"""Coverage/valence guards for the compounding-metrics dashboard route.

The route's dimension tuple, headline map, and valence map are hand-
maintained — these tests keep them in lockstep with each other and with the
aggregator's registered dimensions, and pin the trend_good semantics (arrow
color = valence, never raw direction: a rising noise metric must not render
as improvement).
"""

from __future__ import annotations

import pytest
from flask import Flask

# Imported at MODULE level on purpose: genesis.dashboard.api decorates the
# shared blueprint at import time, and Flask forbids that once the blueprint
# has been registered on any app. A lazy import inside the fixture blows up
# when another test module in the same session registers it first.
from genesis.dashboard.api import blueprint
from genesis.dashboard.routes.eval import (
    _DIMENSIONS,
    _HEADLINE_METRIC,
    _HIGHER_IS_BETTER,
    _trend_good,
)


def test_headline_metric_covers_every_dimension():
    missing = set(_DIMENSIONS) - _HEADLINE_METRIC.keys()
    assert not missing, f"dimensions without a headline metric: {missing}"


def test_valence_map_covers_every_dimension():
    missing = set(_DIMENSIONS) - _HIGHER_IS_BETTER.keys()
    assert not missing, f"dimensions without a valence entry: {missing}"


def test_dimensions_match_aggregator_registration():
    """Every snapshot dimension the weekly aggregator writes (except the
    derived 'composite'/'cognitive_drift' internals) must be displayed."""
    for dim in ("memory", "system", "ego", "cognitive", "procedure",
                "approvals", "goals", "noise"):
        assert dim in _DIMENSIONS, f"aggregator dimension not displayed: {dim}"


@pytest.mark.parametrize(
    ("dim", "trend", "expected"),
    [
        # higher-is-better: up = improving, down = degrading
        ("memory", "up", True),
        ("memory", "down", False),
        # lower-is-better (noise): up = degrading, down = improving
        ("noise", "up", False),
        ("noise", "down", True),
        # valence-ambiguous (approvals): always neutral
        ("approvals", "up", None),
        ("approvals", "down", None),
        # non-directional trends: always neutral
        ("memory", "flat", None),
        ("noise", "insufficient_data", None),
    ],
)
def test_trend_good_valence(dim, trend, expected):
    assert _trend_good(dim, trend) is expected


def test_headline_series_marks_a_definition_break():
    """A redefined headline metric must not be plotted as one continuous line.

    The ego's approval_rate denominator changed on 2026-09-06 (tabled and
    withdrawn stopped counting as rejections, and failed moved into the
    numerator), which raises the rate. An unmarked sparkline would render that
    as improvement caused by nothing but the redefinition.

    This exercises the route's own detect_series_break rather than a copy of
    its loop, so a change to the route cannot leave the test passing.
    """
    from genesis.dashboard.routes.eval import (
        _HEADLINE_DEFN_KEY,
        _HEADLINE_METRIC,
        detect_series_break,
    )

    assert _HEADLINE_DEFN_KEY["ego"] == "approval_rate_defn"
    assert _HEADLINE_METRIC["ego"] == "approval_rate"

    v2 = {"approval_rate": 0.9, "approval_rate_defn": "v2_judged_only"}
    mixed = [
        {"period_end": "2026-08-23", "metrics": {"approval_rate": 0.2}},
        {"period_end": "2026-08-30", "metrics": {"approval_rate": 0.3}},
        {"period_end": "2026-09-06", "metrics": v2},
    ]
    assert detect_series_break(mixed, "approval_rate_defn") == "2026-09-06"

    # A window entirely on one definition has no break — either side of it.
    assert detect_series_break(mixed[:2], "approval_rate_defn") is None
    assert detect_series_break([mixed[2], {"period_end": "x", "metrics": v2}],
                               "approval_rate_defn") is None
    # Dimensions with no registered marker never report a break.
    assert detect_series_break(mixed, None) is None
    assert detect_series_break([], "approval_rate_defn") is None


def test_series_break_is_detected_for_every_registered_defn_key():
    """The registry and the detector must not drift apart."""
    from genesis.dashboard.routes.eval import _HEADLINE_DEFN_KEY, detect_series_break

    for dim, key in _HEADLINE_DEFN_KEY.items():
        snaps = [{"period_end": "a", "metrics": {key: "v1"}},
                 {"period_end": "b", "metrics": {key: "v2"}}]
        assert detect_series_break(snaps, key) == "b", dim


# ── route-level: the trend must not read across a definition break ───────────


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


def _snap(period_end: str, rate: float, defn: str | None = None) -> dict:
    metrics: dict = {"approval_rate": rate}
    if defn is not None:
        metrics["approval_rate_defn"] = defn
    return {"period_end": period_end, "metrics": metrics, "sample_count": 4}


def _compounding(client, by_dim: dict[str, list[dict]]) -> dict:
    """Invoke the real route with get_snapshots mocked; return its JSON.

    Exercises ``metrics_compounding`` end to end rather than re-implementing
    its loop, so a route that stops segmenting the trend — or stops emitting
    series_break_at — fails here.
    """
    from unittest.mock import MagicMock, patch

    async def fake_get_snapshots(_db, *, dimension, period_type, limit):
        assert period_type == "weekly" and limit == 12
        # The route reverses into chronological order, so hand back newest-first.
        return list(reversed(by_dim.get(dimension, [])))

    rt = MagicMock()
    rt.is_bootstrapped = True
    rt._db = MagicMock()
    with patch("genesis.runtime.GenesisRuntime") as MockRT, patch(
        "genesis.db.crud.j9_eval.get_snapshots", new=fake_get_snapshots,
    ):
        MockRT.instance.return_value = rt
        resp = client.get("/api/genesis/metrics/compounding")
    assert resp.status_code == 200, resp.data
    return resp.get_json()["dimensions"]


def test_route_trend_is_computed_inside_one_definition(client):
    """A rise caused only by the redefinition must not render as improvement.

    Chronological ego series: three v1 weeks around 0.1-0.2, then two v2 weeks
    at 0.95 and 0.90. Averaging the whole window compares 0.10 against 0.68 and
    reports `up`/`trend_good: true` — improvement manufactured entirely by the
    denominator change. Inside v2 the rate is FALLING.
    """
    ego = [
        _snap("2026-08-16", 0.10),
        _snap("2026-08-23", 0.10),
        _snap("2026-08-30", 0.20),
        _snap("2026-09-06", 0.95, "v2_judged_only"),
        _snap("2026-09-13", 0.90, "v2_judged_only"),
    ]
    dims = _compounding(client, {"ego": ego})

    assert dims["ego"]["series_break_at"] == "2026-09-06"
    assert dims["ego"]["weeks_of_data"] == 5          # no point is dropped
    assert dims["ego"]["trend_basis_weeks"] == 2      # only v2 is compared
    assert dims["ego"]["trend"] == "down"
    assert dims["ego"]["trend_good"] is False


def test_route_trend_is_insufficient_when_the_new_definition_is_one_week(client):
    """One point on the new definition is not a trend — it is no data yet.

    Under the unsegmented average this window reported `up`/true off a single
    redefined week.
    """
    ego = [
        _snap("2026-08-23", 0.10),
        _snap("2026-08-30", 0.20),
        _snap("2026-09-06", 0.95, "v2_judged_only"),
    ]
    dims = _compounding(client, {"ego": ego})

    assert dims["ego"]["series_break_at"] == "2026-09-06"
    assert dims["ego"]["trend_basis_weeks"] == 1
    assert dims["ego"]["trend"] == "insufficient_data"
    assert dims["ego"]["trend_good"] is None


def test_route_trend_unchanged_for_a_series_with_no_break(client):
    """Blast radius: a dimension that was never redefined keeps its old trend.

    Every point carries `definition: None`, so the segment is the whole series
    and the computation is the pre-existing one.
    """
    ego = [_snap("2026-08-30", 0.20), _snap("2026-09-06", 0.40)]
    dims = _compounding(client, {"ego": ego})

    assert dims["ego"]["series_break_at"] is None
    assert dims["ego"]["trend_basis_weeks"] == 2
    assert dims["ego"]["trend"] == "up"
    assert dims["ego"]["trend_good"] is True
    # A dimension with no snapshots at all still answers.
    assert dims["memory"]["trend"] == "insufficient_data"
    assert dims["memory"]["weeks_of_data"] == 0


def test_latest_definition_segment_keeps_only_the_newest_run():
    """Direct unit coverage of the segmenting helper's edges."""
    from genesis.dashboard.routes.eval import latest_definition_segment

    series = [{"definition": None}, {"definition": None}, {"definition": "v2"}]
    assert latest_definition_segment(series) == [{"definition": "v2"}]
    assert len(latest_definition_segment(series[:2])) == 2
    assert latest_definition_segment([]) == []
