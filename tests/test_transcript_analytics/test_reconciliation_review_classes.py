"""Whole-vector availability requires whole-turn producer ownership."""

import json

import pytest

from genesis.transcript_analytics import query, store

FIELDS = [
    ("input_tokens", "input_tokens"), ("output_tokens", "output_tokens"),
    ("cache_read", "cache_read_input_tokens"), ("cache_create", "cache_creation_input_tokens"),
    ("cache_create_5m", "ephemeral_5m_input_tokens"),
    ("cache_create_1h", "ephemeral_1h_input_tokens"), ("thinking_tokens", "thinking_tokens"),
]


def record(field, sid="parent"):
    usage = {"output_tokens": 7}
    if field.startswith("ephemeral_"):
        usage["cache_creation"] = {field: 7}
    elif field == "thinking_tokens":
        usage["output_tokens_details"] = {field: 7}
    else:
        usage[field] = 7
    return {"type": "assistant", "sessionId": sid, "uuid": "event",
            "message": {"id": "turn", "model": "model", "stop_reason": "end_turn",
                        "content": [{"type": "text", "text": "block"}], "usage": usage}}


def build(tmp_path, files):
    projects, data = tmp_path / "projects", tmp_path / "data"
    for name, records in files.items():
        path = projects / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(item) + "\n" for item in records))
    assert store.ingest(projects, data, lock_path=tmp_path / "writer.lock")["failed"] == 0
    columns, rows = query.run_query(data, "SELECT * FROM turns", live=True)
    assert len(rows) == 1
    return dict(zip(columns, rows[0], strict=True))


@pytest.mark.parametrize("column,field", FIELDS)
@pytest.mark.parametrize("scenario", ["unresolved", "conflicting"])
def test_rejected_owner_withholds_every_token_field(tmp_path, column, field, scenario):
    item = record(field)
    if scenario == "unresolved":
        files = {"a.jsonl": [item], "b.jsonl": [record(field, "other-context")]}
    else:
        item["agentId"] = "child-in-main-source"
        files = {"a.jsonl": [item]}
    turn = build(tmp_path, files)
    assert turn["executor_id"] is None
    assert turn["attribution_status"] == scenario
    assert turn["usage_available"] is False
    assert turn[column] is None


@pytest.mark.parametrize("column,field", FIELDS)
@pytest.mark.parametrize("status", ["confirmed", "inferred"])
def test_supported_owner_preserves_whole_terminal_vector(tmp_path, column, field, status):
    item = record(field)
    files = {"parent.jsonl": [item]}
    if status == "inferred":
        files["parent/subagents/agent-child.jsonl"] = [item]
    turn = build(tmp_path, files)
    assert turn["executor_id"] is not None
    assert turn["attribution_status"] == status
    assert turn["usage_available"] is True
    assert turn[column] == 7
