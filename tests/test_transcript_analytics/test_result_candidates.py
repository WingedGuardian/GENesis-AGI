"""All recognized child identities survive extraction without changing row counts."""

import json

import pyarrow as pa
import pytest

from genesis.transcript_analytics.extract import extract_source
from genesis.transcript_analytics.schema import SCHEMAS

CASES = [
    ({"agentId": None}, "agentId: kid", ["kid"]),
    ({"agentId": ""}, "agentId: kid", ["kid"]),
    ({"agentId": 0}, "agentId: kid", ["kid"]),
    ({"agentId": {}}, "agentId: kid", ["kid"]),
    ({}, "agentId: kid", ["kid"]),
    ({"agentId": "A"}, "agentId: B", ["A", "B"]),
    ({"agentId": "A"}, "agentId: A", ["A"]),
    (None, "agentId: A\nagentId: B", ["A", "B"]),
    (None, [{"type": "text", "text": "ok"}, {"type": "text", "text": "agentId: kid"}], ["kid"]),
    ("agentId: kid", "result without identity", ["kid"]),
    (None, "agentId: A\nagentId: A", ["A"]),
    ({"agentId": "jsonid:kid"}, "result", ["jsonid:6a736f6e69643a6b6964"]),
    ({"agentId": "jsonid:6b6964"}, "result", ["jsonid:6a736f6e69643a366236393634"]),
    ({"agentId": "\ud800"}, "result", ["jsonid:eda080"]),
    ({"agentId": "é"}, "result", ["é"]),
]


@pytest.mark.parametrize("structured,content,expected", CASES)
@pytest.mark.parametrize("placement", ["paired", "late", "orphan"])
def test_recognized_candidates_reconcile_before_scalar_selection(
    tmp_path, structured, content, expected, placement
):
    call = {
        "type": "assistant",
        "sessionId": "parent",
        "uuid": "call",
        "message": {
            "id": "turn",
            "content": [{"type": "tool_use", "id": "spawn", "name": "Agent", "input": {}}],
        },
    }
    result = {
        "type": "user",
        "sessionId": "parent",
        "uuid": "result",
        "toolUseResult": structured,
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "spawn", "content": content}]
        },
    }
    records = (
        [call, result]
        if placement == "paired"
        else [result, call]
        if placement == "late"
        else [result]
    )
    source = tmp_path / "source.jsonl"
    source.write_text("".join(json.dumps(item) + "\n" for item in records))
    rows = extract_source(source, "source.jsonl").tables["tool_calls"]
    results = [row for row in rows if row.get("has_result")]
    assert len(results) == 1
    row = results[0]
    assert row.get("result_agent_id") == (expected[0] if len(expected) == 1 else None)
    assert row["result_agent_ids"] == expected
    converted = pa.Table.from_pylist(rows, schema=SCHEMAS["tool_calls"]).to_pylist()
    assert [item["result_agent_ids"] for item in converted if item["has_result"]] == [expected]
