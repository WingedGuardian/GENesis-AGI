"""One actual spawn call cannot independently confirm contradictory children."""

import json

import pytest

from genesis.transcript_analytics import query, store


def _call(tool="Agent"):
    return {
        "type": "assistant",
        "sessionId": "parent",
        "uuid": "call",
        "message": {
            "id": "turn",
            "model": "model",
            "stop_reason": "tool_use",
            "content": [
                {"type": "tool_use", "id": "spawn", "name": tool, "input": {"description": "work"}}
            ],
        },
    }


def _result(child, number):
    return {
        "type": "user",
        "sessionId": "parent",
        "uuid": f"result-{number}",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "spawn",
                    "content": f"agentId: {child}\nresult",
                }
            ]
        },
    }


def _build(
    tmp_path,
    copies,
    companions=(),
    tool="Agent",
    *,
    placement="paired",
    structured=False,
    is_error=None,
):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    for number, child in enumerate(copies):
        result = _result(child, number)
        if structured:
            result["toolUseResult"] = {"agentId": child}
            result["message"]["content"][0]["content"] = f"nonidentity result {number}"
        if is_error is not None:
            result["message"]["content"][0]["is_error"] = is_error
        records = [_call(tool), result]
        if placement == "late":
            records.reverse()
        elif placement == "orphan":
            (projects / f"result-{number}.jsonl").write_text(json.dumps(result) + "\n")
            records = [_call(tool)]
        (projects / f"copy-{number}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in records)
        )
    for child in companions:
        folder = projects / "parent" / "subagents"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"agent-{child}.jsonl").write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "parent",
                    "agentId": child,
                    "uuid": f"child-{child}",
                    "message": {"content": "work"},
                }
            )
            + "\n"
        )
        (folder / f"agent-{child}.meta.json").write_text(json.dumps({"toolUseId": "spawn"}))
    assert store.ingest(projects, data, lock_path=tmp_path / "writer.lock")["failed"] == 0
    columns, rows = query.run_query(data, "SELECT * FROM actor_lineage", live=True)
    return {
        json.loads(row[columns.index("actor_id")])[1]: dict(zip(columns, row, strict=True))
        for row in rows
    }


@pytest.mark.parametrize("tool", ["Agent", "Task"])
def test_conflicting_result_children_never_both_confirm(tmp_path, tool):
    actors = _build(tmp_path, ["A", "B"], tool=tool)
    assert all(a["parent_actor_id"] is None for a in actors.values())
    assert all(a["lineage_status"] == "conflicting" for a in actors.values())


def test_equal_child_results_keep_unique_producer(tmp_path):
    actor = _build(tmp_path, ["A", "A"])["A"]
    assert actor["lineage_status"] == "confirmed"
    assert json.loads(actor["parent_actor_id"]) == ["parent", None]


def test_competing_companion_children_never_both_confirm(tmp_path):
    actors = _build(tmp_path, ["A"], companions=["A", "B"])
    assert all(a["parent_actor_id"] is None for a in actors.values())
    assert all(a["lineage_status"] == "conflicting" for a in actors.values())


def test_unique_companion_does_not_confirm_contradictory_result_child(tmp_path):
    actors = _build(tmp_path, ["A", "B"], companions=["A"])
    assert actors["A"]["lineage_status"] == "confirmed"
    assert actors["B"]["parent_actor_id"] is None
    assert actors["B"]["lineage_status"] == "conflicting"


@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize("placement", ["paired", "late", "orphan"])
@pytest.mark.parametrize("structured", [False, True])
def test_result_placement_and_encoding_share_producer_proof(tmp_path, tool, placement, structured):
    actor = _build(tmp_path, ["A"], tool=tool, placement=placement, structured=structured)["A"]
    assert actor["lineage_status"] == "confirmed"
    assert json.loads(actor["parent_actor_id"]) == ["parent", None]


@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize("placement", ["paired", "late", "orphan"])
@pytest.mark.parametrize("structured", [False, True])
def test_conflicting_candidate_encodings_all_withhold_edges(tmp_path, tool, placement, structured):
    actors = _build(tmp_path, ["A", "B"], tool=tool, placement=placement, structured=structured)
    assert all(a["parent_actor_id"] is None for a in actors.values())
    assert all(a["lineage_status"] == "conflicting" for a in actors.values())


@pytest.mark.parametrize("is_error", [None, False, True])
def test_unrelated_result_body_or_error_variance_does_not_conflict_child(tmp_path, is_error):
    actor = _build(tmp_path, ["A", "A"], structured=True, is_error=is_error)["A"]
    assert actor["lineage_status"] == "confirmed"


@pytest.mark.parametrize("tool", ["Bash", "mcp__service__tool"])
@pytest.mark.parametrize("placement", ["paired", "late", "orphan"])
@pytest.mark.parametrize("structured", [False, True])
def test_nonspawn_tool_candidate_does_not_manufacture_child(tmp_path, tool, placement, structured):
    assert _build(tmp_path, ["A"], tool=tool, placement=placement, structured=structured) == {}


@pytest.mark.parametrize("placement", ["paired", "late", "orphan"])
@pytest.mark.parametrize("tool", ["Agent", "Task"])
@pytest.mark.parametrize(
    "child,encoded",
    [
        ("jsonid:kid", "jsonid:6a736f6e69643a6b6964"),
        ("jsonid:6b6964", "jsonid:6a736f6e69643a366236393634"),
    ],
)
def test_equal_literal_prefix_result_and_companion_share_one_actor(
    tmp_path, placement, tool, child, encoded
):
    actors = _build(
        tmp_path, [child], companions=[child], tool=tool, placement=placement, structured=True
    )
    assert set(actors) == {encoded}
    assert actors[encoded]["lineage_status"] == "confirmed"
    assert json.loads(actors[encoded]["parent_actor_id"]) == ["parent", None]


@pytest.mark.parametrize("tool", ["Bash", "mcp__service__tool"])
@pytest.mark.parametrize("placement", ["paired", "late", "orphan"])
@pytest.mark.parametrize("child", ["jsonid:kid", "jsonid:6b6964", "\ud800"])
def test_nonspawn_identity_candidates_convert_without_creating_actors(
    tmp_path, tool, placement, child
):
    assert _build(tmp_path, [child], tool=tool, placement=placement, structured=True) == {}
