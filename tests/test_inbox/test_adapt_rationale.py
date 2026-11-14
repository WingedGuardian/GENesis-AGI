"""An optional ADAPT explanation preserves historical parser and routing contracts."""

from dataclasses import asdict, fields, replace

import pytest
import yaml

from genesis.inbox.recommendation import Recommendation, parse_recommendations
from tests.test_autonomy import test_build_lane as build
from tests.test_inbox import test_edge_cases as shared
from tests.test_inbox.test_recommendation import GENESIS_SINGLE, MULTI_ITEM

db = shared.db
config = shared.config
inbox_dir = shared.inbox_dir
writer = shared.writer
mock_invoker = shared.mock_invoker
mock_session_manager = shared.mock_session_manager
monitor = shared.monitor

OLD_FIELDS = (
    "action",
    "next_step",
    "effort",
    "confidence",
    "scope",
    "architecture_impact",
    "timeline",
    "relevance",
    "tool_momentum",
    "tool_activity",
    "tool_maturity",
    "verdict",
    "verdict_reason",
    "build_spec",
    "item_title",
    "classification",
)
MISSING = object()
WARNING = "ADAPT recommendation missing usable why_not_adopt rationale"


def evaluation(action="ADAPT", rationale=MISSING):
    data = {"action": action, "next_step": "Compare the adapter interface", "scope": "V4"}
    if rationale is not MISSING:
        data["why_not_adopt"] = rationale
    return (
        "## 1. Synthetic tool\n**Source:** https://example.com/first\n\n"
        "### Recommendation\n```yaml\n" + yaml.safe_dump(data) + "```\n"
    )


@pytest.mark.parametrize(
    "value", [MISSING, None, "", " \n\t ", 0, 7, False, True, ["private marker"], {"x": 1}]
)
@pytest.mark.parametrize("action", ["ADAPT", " aDaPt "])
def test_missing_or_invalid_rationale_warns_without_rejecting(value, action, caplog):
    recs = parse_recommendations(evaluation(action, value))
    assert len(recs) == 1
    assert recs[0].why_not_adopt is None
    assert recs[0].action == action.strip()
    assert recs[0].next_step == "Compare the adapter interface"
    assert [r.getMessage() for r in caplog.records] == [WARNING]


@pytest.mark.parametrize(
    "action",
    [
        "ADOPT",
        "ADAPT",
        "WATCH",
        "IGNORE",
        "BUILD",
        "adopt",
        "explore",
        "bookmark",
        "potential_skip",
    ],
)
@pytest.mark.parametrize(
    "value", [MISSING, "  CLI integration lacks the required offline mode.\n" * 80]
)
def test_all_actions_preserve_full_string_and_existing_decisions(action, value, caplog):
    rec = parse_recommendations(evaluation(action, value))[0]
    assert rec.why_not_adopt == (None if value is MISSING else value.strip())
    assert rec.action == action
    assert rec.is_actionable == (action not in {"IGNORE", "BUILD", "potential_skip"})
    assert len(caplog.records) == (action == "ADAPT" and value is MISSING)


def test_original_positional_prefix_and_historical_output_are_preserved():
    assert tuple(f.name for f in fields(Recommendation)) == OLD_FIELDS + ("why_not_adopt",)
    old_values = (
        "ADAPT",
        "Compare the interface",
        "Medium",
        "high",
        "V4",
        "extends",
        None,
        None,
        "high",
        "medium",
        "low",
        None,
        None,
        None,
        "Tool",
        "genesis",
    )
    positional = Recommendation(*old_values)
    assert tuple(asdict(positional)[key] for key in OLD_FIELDS) == old_values
    assert positional.why_not_adopt is None
    legacy = parse_recommendations(GENESIS_SINGLE)[0]
    assert asdict(legacy) == dict(
        zip(
            OLD_FIELDS,
            (
                "ADAPT",
                "Prototype a metric-gated selection pattern for surplus code tasks",
                "Medium",
                "medium",
                "V4",
                "extends",
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                "Evo — Autoresearch Loop for Codebase Optimization",
                "genesis",
            ),
            strict=True,
        )
    ) | {"why_not_adopt": None}
    multi = parse_recommendations(MULTI_ITEM)
    assert len(multi) == 4
    assert all(rec.why_not_adopt is None for rec in multi)


@pytest.mark.asyncio
@pytest.mark.parametrize("lane_enabled", [False, True])
@pytest.mark.parametrize("rationale", [MISSING, "The integration API lacks offline operation."])
async def test_scan_parser_followups_and_build_ownership_stay_compatible(
    lane_enabled,
    rationale,
    monitor,
    inbox_dir,
    mock_invoker,
    db,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr("genesis.autonomy.build_lane._PLANS_DIR", tmp_path / "plans")
    gate, dispatcher = build.FakeGate(), build.FakeDispatcher()
    lane = build._lane(db, gate=gate, dispatcher=dispatcher, enabled=lane_enabled)
    monitor.set_build_lane(lane)
    text = (
        evaluation(rationale=rationale)
        + "\n"
        + build._eval_text(
            "Synthetic capability",
            "build",
            build_spec=build._GOOD_SPEC,
        )
    )
    # Both recommendations retain their new metadata; only existing verdicts route.
    recs = parse_recommendations(text)
    assert recs[0].why_not_adopt == (None if rationale is MISSING else rationale)
    assert recs[1].why_not_adopt is None
    mock_invoker.run.return_value = replace(shared._success_output(), text=text)
    (inbox_dir / "Note.md").write_text("https://example.com/first")
    result = await monitor.check_once()
    assert result.batches_dispatched == 1
    rows = await (
        await db.execute(
            "SELECT content,kind,domain,source_session FROM follow_ups ORDER BY content"
        )
    ).fetchall()
    assert len(rows) == (1 if lane_enabled else 2)
    assert rows[0]["content"].startswith("[ADAPT] Synthetic tool:")
    assert rows[0]["kind"] == "follow_up"
    assert all(r["domain"] == "internal" and r["source_session"] == "cc-sess-1" for r in rows)
    candidates = await (await db.execute("SELECT verdict FROM build_candidates")).fetchall()
    assert [r["verdict"] for r in candidates] == (["build"] if lane_enabled else [])
    assert len(gate.ensure_calls) == int(lane_enabled)
    assert dispatcher.calls == []
    items = await (
        await db.execute("SELECT status,evaluated_content,response_path FROM inbox_items")
    ).fetchall()
    assert len(items) == 1 and items[0]["status"] == "completed"
    assert items[0]["evaluated_content"] == "https://example.com/first"
    assert (await monitor.check_once()).batches_dispatched == 0
