"""#4335: an ``adapt_`` action is routed as ADAPT, so it must warn on a missing rationale.

Follow-up routing matches a *normalised* action key (underscores to spaces,
trimmed) while the parser's missing-``why_not_adopt`` warning compared the raw
casefolded action.  An evaluator emitting ``action: adapt_`` was therefore
routed as an ADAPT follow-up but never triggered the warning.  Parser and
routing now share one helper: ``genesis.inbox.recommendation.normalise_action``.
"""

from __future__ import annotations

import pytest

from genesis.inbox.recommendation import parse_recommendations
from tests.test_inbox import test_edge_cases as shared
from tests.test_inbox.test_adapt_rationale import WARNING, evaluation

# Fixtures re-exported the same way test_adapt_rationale.py does it.
db = shared.db
config = shared.config
inbox_dir = shared.inbox_dir
writer = shared.writer
mock_invoker = shared.mock_invoker
mock_session_manager = shared.mock_session_manager
monitor = shared.monitor


def test_adapt_underscore_without_rationale_warns(caplog):
    """``adapt_`` is routed as ADAPT, so a missing rationale must warn (#4335).

    Failing-first on current main: the parser compared the raw casefolded
    action (``"adapt_" == "adapt"`` is False), so no warning was logged even
    though routing normalises the same action to ADAPT.
    """
    recs = parse_recommendations(evaluation("adapt_"))
    assert len(recs) == 1
    assert recs[0].why_not_adopt is None
    assert recs[0].is_actionable is True  # routed as an ADAPT follow-up
    assert [r.getMessage() for r in caplog.records] == [WARNING]


@pytest.mark.parametrize(
    "raw",
    ["ADAPT", "adapt", "adapt_", " adapt_ ", "_ADAPT_", "aDaPt_"],
)
def test_normalise_action_is_the_shared_routing_key(raw):
    """One helper normalises every action variant to the routing key."""
    from genesis.inbox.recommendation import normalise_action

    assert normalise_action(raw) == "adapt"


@pytest.mark.asyncio
async def test_adapt_underscore_routes_as_adapt_follow_up_and_warns(monitor, db, caplog):
    """End-to-end #4335 contract: routed as ADAPT *and* warned, from one normalisation."""
    created = await monitor._create_follow_ups_from_eval(
        evaluation_text=evaluation("adapt_"),
        batch_id="batch-4335",
        source_files=["Genesis.md"],
    )
    assert created == 1
    rows = [
        dict(r)
        for r in await (
            await db.execute(
                "SELECT content, kind FROM follow_ups WHERE source = 'inbox_evaluation'",
            )
        ).fetchall()
    ]
    # The label is the raw action upper-cased ("[ADAPT_]" here); routing
    # and the warning are the #4335 contract, not the label spelling.
    assert rows[0]["content"].startswith("[ADAPT")
    assert "Synthetic tool" in rows[0]["content"]
    assert rows[0]["kind"] == "follow_up"
    parser_warnings = [
        r.getMessage() for r in caplog.records if r.name == "genesis.inbox.recommendation"
    ]
    assert parser_warnings == [WARNING]
