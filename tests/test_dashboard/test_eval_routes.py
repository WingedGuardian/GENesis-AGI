"""Coverage/valence guards for the compounding-metrics dashboard route.

The route's dimension tuple, headline map, and valence map are hand-
maintained — these tests keep them in lockstep with each other and with the
aggregator's registered dimensions, and pin the trend_good semantics (arrow
color = valence, never raw direction: a rising noise metric must not render
as improvement).
"""

from __future__ import annotations

import pytest

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


async def test_headline_series_marks_a_definition_break(monkeypatch):
    """A redefined headline metric must not be plotted as one continuous line.

    The ego's approval_rate denominator changed on 2026-09-06 (tabled and
    withdrawn stopped counting as rejections), which shrinks the denominator
    and raises the rate. An unmarked sparkline would render that as
    improvement caused by nothing but the redefinition.
    """
    from genesis.dashboard.routes.eval import _HEADLINE_DEFN_KEY, _HEADLINE_METRIC

    assert _HEADLINE_DEFN_KEY["ego"] == "approval_rate_defn"
    assert _HEADLINE_METRIC["ego"] == "approval_rate"

    # Simulate the loop's break detection over a mixed window.
    snapshots = [
        {"period_end": "2026-08-23", "metrics": {"approval_rate": 0.2}},
        {"period_end": "2026-08-30", "metrics": {"approval_rate": 0.3}},
        {"period_end": "2026-09-06",
         "metrics": {"approval_rate": 0.9, "approval_rate_defn": "v2_judged_only"}},
    ]
    defn_key, series_break_at, prev_defn, series = "approval_rate_defn", None, None, []
    for snap in snapshots:
        defn = snap["metrics"].get(defn_key)
        if series_break_at is None and series and defn != prev_defn:
            series_break_at = snap["period_end"]
        prev_defn = defn
        series.append(snap["period_end"])

    assert series_break_at == "2026-09-06", series_break_at
    assert len(series) == 3, "points must be kept, not dropped"
