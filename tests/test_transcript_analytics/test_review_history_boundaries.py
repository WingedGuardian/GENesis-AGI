"""Historical unknowns and ambiguous physical observations stay distinguishable."""

import contextlib
import json
import shutil
from pathlib import Path

import pytest
from test_deterministic_views import _connection, _rows

from genesis.transcript_analytics import query, store
from genesis.transcript_analytics.extract import TABLES


def empty():
    return {name: [] for name in TABLES}


@pytest.mark.parametrize("identities", [
    [("a:b", "c"), ("a", "b:c")], [(None, "x"), ("", "x")],
])
def test_unknown_agents_keep_distinct_structured_identity(identities):
    rows = empty()
    rows["agents"] = [
        {"source_file": "history.jsonl", "agent_id": agent, "tool_use_id": tool,
         "scrub_failed": index == 1}
        for index, (agent, tool) in enumerate(identities)
    ]
    with contextlib.closing(_connection(rows)) as con:
        assert con.sql("SELECT count(*) FROM agents").fetchone() == (2,)
        assert con.sql("SELECT count(*) FROM agents WHERE scrub_failed").fetchone() == (1,)


def test_unknown_historical_actor_does_not_block_current_lineage():
    rows = empty()
    child = {**_rows()["fragments"][0], "source_role": "child"}
    rows["fragments"] = [child]
    rows["agents"] = [{"source_file": "old.jsonl", "tool_use_id": "spawn"}]
    with contextlib.closing(_connection(rows)) as con:
        assert con.sql("SELECT actor_id FROM actor_lineage").fetchall() == [(child["actor_id"],)]
        assert con.sql("SELECT actor_id FROM raw_agents").fetchall() == [(None,)]


@pytest.mark.parametrize("hashes,expected", [
    ([None], None), (["a", None], None), (["a", "b", None], True), (["a"], False),
])
def test_turn_content_unknowns_remain_excluded_through_aggregation(hashes, expected):
    rows = empty()
    rows["fragments"] = [
        {**_rows()["fragments"][0], "source_file": f"copy-{index}.jsonl", "content_hash": value}
        for index, value in enumerate(hashes)
    ]
    with contextlib.closing(_connection(rows)) as con:
        assert con.sql("SELECT content_conflict FROM turns").fetchone() == (expected,)
        assert con.sql("SELECT included,excluded FROM metric_coverage WHERE metric='turn_content'").fetchone() == (
            int(expected is False), int(expected is not False),
        )
        if expected is not False:
            assert con.sql("SELECT n_tool_use,usage_available FROM turns").fetchone() == (None, False)


@pytest.mark.parametrize("check", ["versions", "turn_content"])
def test_genuine_history_manifest_versions_and_content_coverage(tmp_path, check):
    data = tmp_path / "data"
    shutil.copytree(Path(__file__).with_name("fixtures") / "flat-v1", data)
    manifest = tmp_path / "query.json"
    query.run_query(data, "SELECT count(*) FROM turns", manifest_path=manifest)
    if check == "versions":
        assert json.loads(manifest.read_text())["versions"]["views_version"] == query.VIEWS_VERSION
        return
    with query.read_connection(data) as con:
        table = {"turn_content": "turns", "hook_content": "hooks", "event_content": "events"}[check]
        count = con.sql(f"SELECT count(*) FROM {table}").fetchone()[0]
        assert count > 0
        assert con.sql("SELECT included,excluded FROM metric_coverage WHERE metric=?", params=[check]).fetchone() == (0, count)


@pytest.mark.parametrize("conflict", [False, True])
def test_spawn_result_cannot_confirm_lineage_from_conflicting_context(tmp_path, conflict):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    call = {"type": "assistant", "uuid": "call", "sessionId": "parent", "message": {
        "id": "turn", "content": [{"type": "tool_use", "id": "spawn", "name": "Agent", "input": {}}],
    }}
    result = {"type": "user", "uuid": "result", "sessionId": "wrong" if conflict else "parent",
              "message": {"content": [{"type": "tool_result", "tool_use_id": "spawn",
                                         "content": "agentId: target\nresult"}]}}
    (projects / "parent.jsonl").write_text(json.dumps(call) + "\n")
    child = projects / "parent/subagents/agent-path.jsonl"
    child.parent.mkdir(parents=True)
    child.write_text(json.dumps(result) + "\n")
    store.ingest(projects, data)
    with query.read_connection(data) as con:
        parents = con.sql("SELECT parent_actor_id FROM actor_lineage WHERE actor_id=?",
                          params=['["parent","target"]']).fetchall()
        assert parents == ([] if conflict else [('["parent",null]',)])
        assert con.sql("SELECT context_conflict FROM raw_tool_calls WHERE has_result").fetchone() == (conflict,)


@pytest.mark.parametrize("kind", ["call", "result"])
def test_same_record_tool_variants_are_stable_in_either_input_order(kind):
    rows = empty()
    fragment = _rows()["fragments"][0]
    rows["fragments"] = [fragment]
    common = {"source_file": fragment["source_file"], "session_id": "s", "context_session_id": "s",
              "actor_id": '["s",null]', "context_conflict": False, "source_role": "main",
              "tool_use_id": "one", "call_uuid": fragment["uuid"], "message_id": fragment["message_id"],
              "line_no_call": 1, "call_hash": "stable", "tool": "Bash", "scrub_failed": False,
              "has_result": kind == "result"}
    rows["tool_calls"] = [
        {**common, **({"call_hash": value, "command": value} if kind == "call" else
                     {"line_no_result": 2, "result_hash": value, "error_text": value})}
        for value in ("a", "b")
    ]
    results = []
    for reverse in (False, True):
        with contextlib.closing(_connection(rows, reverse)) as con:
            results.append(con.sql("SELECT command,error_text,content_conflict,result_conflict FROM tool_calls").fetchall())
    assert results[0] == results[1]
    assert results[0][0][2 if kind == "call" else 3] is True
