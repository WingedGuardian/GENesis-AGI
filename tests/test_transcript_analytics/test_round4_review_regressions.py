"""Round-4 review regressions: agent-ID scrubbing, tri-state sidechain flags,
structured API-error status, and structured JSON-RPC error envelopes."""

import json

import pytest

from genesis.transcript_analytics.classify import classify
from genesis.transcript_analytics.extract import extract_source

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret


def _write(tmp_path, records):
    path = tmp_path / "source.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return extract_source(path, path.name)


def _spawn_result(structured, content):
    return [
        {
            "type": "assistant",
            "sessionId": "parent",
            "uuid": "call",
            "message": {
                "id": "turn",
                "content": [{"type": "tool_use", "id": "spawn", "name": "Agent", "input": {}}],
            },
        },
        {
            "type": "user",
            "sessionId": "parent",
            "uuid": "result",
            "toolUseResult": structured,
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "spawn", "content": content}]
            },
        },
    ]


@pytest.mark.parametrize(
    "structured,content",
    [
        ({"agentId": TOKEN}, "result"),
        (None, f"agentId: {TOKEN}"),
        (f"agentId: {TOKEN}", "result"),
        ({"agentId": TOKEN}, f"agentId: {TOKEN}"),
    ],
)
def test_credential_shaped_agent_id_is_not_persisted(tmp_path, structured, content):
    result = _write(tmp_path, _spawn_result(structured, content))
    row = next(r for r in result.tables["tool_calls"] if r.get("has_result"))
    assert row["result_agent_ids"] == []
    assert row["result_agent_id"] is None
    assert TOKEN not in json.dumps(result.tables)


def test_ordinary_agent_id_survives_next_to_rejected_one(tmp_path):
    result = _write(tmp_path, _spawn_result({"agentId": "a05de3b28c4029fea"}, f"agentId: {TOKEN}"))
    row = next(r for r in result.tables["tool_calls"] if r.get("has_result"))
    assert row["result_agent_ids"] == ["a05de3b28c4029fea"]
    assert row["result_agent_id"] == "a05de3b28c4029fea"


@pytest.mark.parametrize(
    "value,expected",
    [
        (True, True),
        (False, False),
        ("false", None),
        ("true", None),
        (1, None),
        (0, None),
        (None, None),
        ("missing", None),
    ],
)
def test_is_sidechain_keeps_only_json_booleans(tmp_path, value, expected):
    assistant = {
        "type": "assistant",
        "sessionId": "s",
        "uuid": "a",
        "message": {
            "id": "m",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "tool_use", "id": "t", "name": "Bash", "input": {}},
            ],
        },
    }
    user = {
        "type": "user",
        "sessionId": "s",
        "uuid": "u",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]},
    }
    orphan = {
        "type": "user",
        "sessionId": "s",
        "uuid": "o",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
    }
    for record in (assistant, user, orphan):
        if value != "missing":
            record["isSidechain"] = value
    t = _write(tmp_path, [assistant, user, orphan]).tables
    assert t["fragments"] and t["tool_calls"]
    assert {f["is_sidechain"] for f in t["fragments"]} == {expected}
    assert {c["is_sidechain"] for c in t["tool_calls"]} == {expected}


def test_structured_api_error_status_is_projected_and_retained(tmp_path):
    record = {
        "type": "system",
        "subtype": "api_error",
        "sessionId": "s",
        "uuid": "e",
        "level": "error",
        "status": {
            "status": 529,
            "headers": {"authorization": "Bearer opaquecredential123456"},
            "error": {"type": "overloaded_error", "message": "Overloaded"},
        },
    }
    event = _write(tmp_path, [record]).tables["events"][0]
    assert event["status"] == 529
    detail = json.loads(event["detail"])
    assert detail["status"]["status"] == 529
    assert detail["status"]["error"]["type"] == "overloaded_error"
    assert "headers" not in detail["status"]
    assert "opaquecredential123456" not in event["detail"]
    assert event["scrub_failed"] is False


@pytest.mark.parametrize(
    "status,expected",
    [(429, 429), ({"status": True}, None), ("529", None), ({"status": "529"}, None), ({}, None)],
)
def test_api_error_status_integer_projection(tmp_path, status, expected):
    record = {
        "type": "system",
        "subtype": "api_error",
        "sessionId": "s",
        "uuid": "e",
        "status": status,
    }
    assert _write(tmp_path, [record]).tables["events"][0]["status"] == expected


@pytest.mark.parametrize(
    "raw",
    [
        '{"jsonrpc":"2.0","id":1,"error":{"code":-32602,"message":"Invalid params"}}',
        '{ "error" : { "code" : -32000 , "message" : "server error", "data": {} } }',
    ],
)
@pytest.mark.parametrize("flag", [True, False, None])
def test_structured_jsonrpc_error_object(raw, flag):
    assert classify(raw, is_error=flag, denial_kind=None) == (
        (None, None) if flag is False else ("mcp_error", "flag" if flag else "text")
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"error":{"message":"no code"}}',
        '{"error":{"code":"-32602","message":"string code"}}',
        '{"error":{"code":true,"message":"bool code"}}',
        '{"error":{"code":1}}',
        '{"error":null}',
        '{"error":{}}',
    ],
)
def test_unvalidated_error_mappings_are_not_structural(raw):
    assert classify(raw, is_error=None, denial_kind=None) == (None, None)
