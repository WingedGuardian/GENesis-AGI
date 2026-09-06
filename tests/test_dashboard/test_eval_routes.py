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
