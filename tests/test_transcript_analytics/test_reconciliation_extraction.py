"""Every-observation prerequisites for turn and spawning-call reconciliation."""

import json

import pyarrow as pa
import pytest

from genesis.transcript_analytics import extract
from genesis.transcript_analytics.schema import SCHEMAS


def _record(**overrides):
    value = {
        "type": "assistant",
        "sessionId": "parent",
        "uuid": "event",
        "timestamp": "2026-10-07T00:00:00Z",
        "message": {"id": "turn", "model": "model", "content": []},
    }
    value.update(overrides)
    return value


def _rows(tmp_path, records, name="parent.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    result = extract.extract_source(path, name)
    for table, rows in result.tables.items():
        pa.Table.from_pylist(rows, schema=SCHEMAS[table])
    return result.tables


@pytest.mark.parametrize("provider", [None, "", 0, [], {}])
@pytest.mark.parametrize("uuid", [None, "event"])
def test_ordinary_missing_provider_keeps_fragment_and_tool(tmp_path, provider, uuid):
    record = _record(uuid=uuid)
    record["message"].update(
        id=provider,
        content=[
            {
                "type": "tool_use",
                "id": "call",
                "name": "Bash",
                "input": {"command": "true"},
            }
        ],
    )
    rows = _rows(tmp_path, [record])
    assert len(rows["fragments"]) == 1
    assert rows["fragments"][0]["message_id"] is None
    assert rows["tool_calls"][0]["tool_use_id"] == "call"


@pytest.mark.parametrize(
    "record",
    [
        _record(agentId="child"),
        {
            "type": "attachment",
            "sessionId": "parent",
            "agentId": "child",
            "attachment": {"type": "hook_success"},
        },
        {
            "type": "system",
            "subtype": "compact_boundary",
            "sessionId": "parent",
            "agentId": "child",
        },
    ],
)
def test_main_placement_keeps_contradictory_label_and_marks_conflict(tmp_path, record):
    rows = _rows(tmp_path, [record])
    observed = [row for table in ("fragments", "hooks", "events") for row in rows[table]]
    assert len(observed) == 1
    assert observed[0]["agent_id"] == "child"
    assert observed[0]["actor_id"] == '["parent",null]'
    assert observed[0]["context_conflict"] is True


def test_request_absence_hash_is_explicit_and_typed(tmp_path):
    records = [
        _record(),
        _record(requestId=None),
        _record(requestId=""),
        _record(requestId=1),
        _record(requestId="1"),
    ]
    rows = _rows(tmp_path, records)["fragments"]
    hashes = [row["request_id_hash"] for row in rows]
    assert hashes[0] is not None
    assert hashes[0] == hashes[1]
    assert len(set(hashes)) == 4


@pytest.mark.parametrize(
    "field",
    [
        ("input_tokens",),
        ("output_tokens",),
        ("cache_read_input_tokens",),
        ("cache_creation_input_tokens",),
        ("cache_creation", "ephemeral_5m_input_tokens"),
        ("cache_creation", "ephemeral_1h_input_tokens"),
        ("output_tokens_details", "thinking_tokens"),
    ],
)
@pytest.mark.parametrize("value", [-1, 0, 2**63 - 1, 2**63, True])
def test_usage_is_nonnegative_int64_for_all_seven_fields(tmp_path, field, value):
    names = {
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cache_read_input_tokens": "cache_read",
        "cache_creation_input_tokens": "cache_create",
        "ephemeral_5m_input_tokens": "cache_create_5m",
        "ephemeral_1h_input_tokens": "cache_create_1h",
        "thinking_tokens": "thinking_tokens",
    }
    record = _record()
    usage = record["message"]["usage"] = {}
    if len(field) == 2:
        usage = usage.setdefault(field[0], {})
    usage[field[-1]] = value
    row = _rows(tmp_path, [record])["fragments"][0]
    expected = value if type(value) is int and 0 <= value < 2**63 else None
    assert row[names[field[-1]]] == expected


def test_orphan_text_retains_candidate_without_assigning_tool_or_actor(tmp_path):
    record = {
        "type": "user",
        "sessionId": "parent",
        "message": {
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "remote-call",
                    "content": "agentId: child-1\n",
                }
            ]
        },
    }
    row = _rows(tmp_path, [record])["tool_calls"][0]
    assert row["tool"] is None
    assert row["result_agent_id"] == "child-1"
    assert row["actor_id"] == '["parent",null]'
