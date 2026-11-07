"""Bounded parser closeout: uncertain attribution and malformed producer records."""

import hashlib
import json

import pytest

from genesis.transcript_analytics import extract
from genesis.transcript_analytics.classify import parse_hook_block

TOKEN = "ghp_" + "A1" * 18  # pragma: allowlist secret


def _write(tmp_path, records, *, name="source.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return path


def _event(**fields):
    return {"type": "system", "subtype": "turn_duration", "sessionId": "session", **fields}


@pytest.mark.parametrize(
    "command,expected",
    [
        ("python3 -u /x/hook.py", "<unknown>"),
        ("python3 -W ignore /x/hook.py", "<unknown>"),
        ("/bin/bash -e /x/hook.sh", "<unknown>"),
        ("node -e 'synthetic()'", "<unknown>"),
        ("bash -lc 'synthetic'", "<unknown>"),
        ("python3 -u /x/genesis-hook guard.py", "<unknown>"),
        ('python3 "$HOOK"', "<unknown>"),
        ('"/x/hook with spaces.py"', "<unknown>"),
        ("/x/guard.sh ; /x/other.sh", "<unknown>"),
        ("python3 /x/*.py", "<unknown>"),
        ("python3", "<unknown>"),
        ("/bin/bash -c 'synthetic'", "<inline>"),
        ("/usr/bin/python3 /x/hook.py", "hook.py"),
        ("python3 /x/genesis-hook guard.py", "guard.py"),
        ("echo /x/genesis-hook argument.py", "echo"),
    ],
)
def test_hook_names_use_only_supported_command_positions(command, expected):
    result = parse_hook_block(f"PreToolUse:Bash hook error: [{command}]: synthetic")
    assert result["hook_script"] == expected
    assert result["hook_command"] == command


def _calls(result_timestamp, *, error=None):
    result = {"type": "tool_result", "tool_use_id": "call", "content": error or "done"}
    if error:
        result["is_error"] = True
    return [
        {
            "type": "assistant", "sessionId": "session",
            "timestamp": "2026-01-01T00:00:01Z",
            "message": {"id": "turn", "content": [
                {"type": "tool_use", "id": "call", "name": "Bash", "input": {}}
            ] * 2},
        },
        {"type": "user", "sessionId": "session", "timestamp": result_timestamp,
         "message": {"content": [result]}},
    ]


def test_unknown_hook_name_retains_scrubbed_command(tmp_path):
    error = f"PreToolUse:Bash hook error: [python3 -u /x/hook.py --token {TOKEN}]: synthetic"
    path = _write(tmp_path, _calls("2026-01-01T00:00:02Z", error=error))
    result = extract.extract_source(path, path.name)
    assert len(result.tables["tool_calls"]) == 2
    for row in result.tables["tool_calls"]:
        assert row["hook_script"] == "<unknown>"
        assert row["hook_command"].startswith("python3 -u /x/hook.py")
    assert TOKEN not in json.dumps(result.tables)


@pytest.mark.parametrize(
    "timestamp,expected",
    [("2026-01-01T00:00:00Z", None), ("2026-01-01T00:00:00.999999Z", None),
     ("2026-01-01T00:00:01Z", 0), ("2026-01-01T00:00:02Z", 1000)],
)
def test_first_and_additional_call_latencies_are_nonnegative(tmp_path, timestamp, expected):
    path = _write(tmp_path, _calls(timestamp))
    rows = extract.extract_source(path, path.name).tables["tool_calls"]
    assert len(rows) == 2
    assert [row["latency_ms"] for row in rows] == [expected, expected]


@pytest.mark.parametrize("value,expected", [(-1, None), (0, 0), (1, 1),
                                           (True, None), (2**63, None), ("3", None)])
def test_duration_and_count_domains_preserve_signed_exit_codes(tmp_path, value, expected):
    path = _write(tmp_path, [
        _event(durationMs=value, messageCount=value),
        {"type": "attachment", "sessionId": "session", "attachment": {
            "type": "hook_response", "durationMs": value, "exitCode": -9,
        }},
    ], name="agent-child.jsonl")
    path.with_name("agent-child.meta.json").write_text(json.dumps({"spawnDepth": value}))
    tables = extract.extract_source(path, path.name).tables
    assert tables["events"][0]["duration_ms"] == expected
    assert tables["events"][0]["message_count"] == expected
    assert tables["hooks"][0]["duration_ms"] == expected
    assert tables["hooks"][0]["exit_code"] == -9
    assert tables["agents"][0]["spawn_depth"] == expected


def test_recursive_json_line_retains_surrounding_valid_records(tmp_path):
    path = _write(tmp_path, [_event(uuid="before")])
    with path.open("a") as stream:
        stream.write("[" * 10000 + "0" + "]" * 10000 + "\n")
        stream.write(json.dumps(_event(uuid="after")) + "\n")
    result = extract.extract_source(path, path.name)
    assert [row["uuid"] for row in result.tables["events"]] == ["before", "after"]
    assert result.stats["malformed"] == 1
    assert result.chronology_known is False


def test_recursive_agent_sidecar_preserves_transcript_rows(tmp_path):
    path = _write(tmp_path, [_event(uuid="kept")], name="agent-child.jsonl")
    path.with_name("agent-child.meta.json").write_text("[" * 10000 + "0" + "]" * 10000)
    result = extract.extract_source(path, path.name)
    assert [row["uuid"] for row in result.tables["events"]] == ["kept"]
    assert result.tables["agents"] == []


@pytest.mark.parametrize("subtype", ["model_refusal_fallback", "model_refusal_no_fallback"])
def test_refusal_diagnostics_are_selected_and_scrubbed(tmp_path, subtype):
    fields = {key: "synthetic " + TOKEN for key in (
        "originalModel", "requestId", "apiRefusalCategory", "apiRefusalExplanation",
    )}
    path = _write(tmp_path, [_event(subtype=subtype, **fields)])
    rows = extract.extract_source(path, path.name).tables["events"]
    assert len(rows) == 1 and rows[0]["subtype"] == subtype
    detail = json.loads(rows[0]["detail"])
    assert set(fields) <= detail.keys()
    assert all(detail[key].startswith("synthetic ") for key in fields)
    assert TOKEN not in rows[0]["detail"]
    assert rows[0]["scrub_failed"] is False


def test_refusal_diagnostic_scrub_failure_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "scrub_json", lambda _value: (None, True))
    path = _write(tmp_path, [_event(subtype="model_refusal_no_fallback", requestId="synthetic")])
    (row,) = extract.extract_source(path, path.name).tables["events"]
    assert row["detail"] is None and row["scrub_failed"] is True


@pytest.mark.parametrize("explicit", [False, True])
def test_later_append_does_not_change_captured_chronology(tmp_path, monkeypatch, explicit):
    path = _write(tmp_path, [_event(timestamp="2026-01-01T00:00:00Z")])
    captured = path.read_bytes()
    original = extract._complete_lines

    def append_after_read(path, stop_at, counter):
        yield from original(path, stop_at, counter)
        with path.open("ab") as stream:
            stream.write(json.dumps(_event(timestamp="2026-01-02T00:00:00Z")).encode() + b"\n")

    monkeypatch.setattr(extract, "_complete_lines", append_after_read)
    result = extract.extract_source(path, path.name, len(captured) if explicit else None)
    assert result.chronology_known is True
    assert result.stats["bytes_read"] == len(captured)
    assert result.content_sha256 == hashlib.sha256(captured).hexdigest()
    assert len(result.tables["events"]) == 1


def test_torn_captured_suffix_is_hashed_but_not_parsed(tmp_path):
    path = _write(tmp_path, [_event(timestamp="2026-01-01T00:00:00Z")])
    complete = path.read_bytes()
    captured = complete + b'{"torn":'
    path.write_bytes(captured)
    result = extract.extract_source(path, path.name)
    assert result.chronology_known is False
    assert result.stats["bytes_read"] == len(complete)
    assert result.content_sha256 == hashlib.sha256(captured).hexdigest()
    assert len(result.tables["events"]) == 1
