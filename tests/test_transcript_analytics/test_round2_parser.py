"""Round-two provider, Unicode and serialization contract regressions."""

import json

import pyarrow as pa
import pytest

from genesis.transcript_analytics import extract, scrub
from genesis.transcript_analytics.classify import classify
from genesis.transcript_analytics.schema import SCHEMAS


def test_string_population_normalizes_every_schema_column():
    from genesis.transcript_analytics.identity import (
        IDENTITY_FIELDS,
        decode_identity,
        normalize_rows,
    )

    tables = {}
    for name, schema in SCHEMAS.items():
        row = {}
        for column in schema:
            if pa.types.is_string(column.type):
                row[column.name] = (
                    "fsbytes:literal" if column.name == "source_file" else "bad\ud800"
                )
        tables[name] = [row]
    normalize_rows(tables)
    for name, rows in tables.items():
        pa.Table.from_pylist(rows, schema=SCHEMAS[name])
        for key in IDENTITY_FIELDS & rows[0].keys():
            assert decode_identity(rows[0][key]) == "bad\ud800"


def test_all_schema_strings_are_arrow_safe_and_identity_is_lossless(tmp_path):
    from genesis.transcript_analytics.identity import decode_identity, encode_identity

    path = tmp_path / "agent-worker.jsonl"
    records = []
    for value in ("\ud800", "\ud801", "jsonid:eda080"):
        records.extend(
            [
                {
                    "type": "assistant",
                    "sessionId": value,
                    "agentId": value,
                    "requestId": value,
                    "uuid": value,
                    "timestamp": "2025-01-01T00:00:00Z",
                    "message": {
                        "id": value,
                        "model": "bad\ud800",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": value,
                                "name": "Bash",
                                "input": {"command": "bad\ud800", "file_path": "bad\ud800"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "timestamp": "2025-01-01T00:00:01Z",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": value,
                                "is_error": True,
                                "content": "bad\ud800",
                            }
                        ]
                    },
                },
                {
                    "type": "attachment",
                    "uuid": value,
                    "attachment": {"type": "hook_success", "command": "bad\ud800"},
                },
                {"type": "system", "subtype": "api_error", "uuid": value, "content": "bad\ud800"},
                {"type": "ai-title", "sessionId": value, "aiTitle": "bad\ud800"},
            ]
        )
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    path.with_suffix(".meta.json").write_text(json.dumps({"description": "bad\ud800"}))
    result = extract.extract_source(path, "worker.jsonl")
    for name, rows in result.tables.items():
        pa.Table.from_pylist(rows, schema=SCHEMAS[name])
    identities = [r["uuid"] for r in result.tables["fragments"]]
    assert len(set(identities)) == 3
    assert [decode_identity(v) for v in identities] == ["\ud800", "\ud801", "jsonid:eda080"]
    assert decode_identity(encode_identity("valid🙂")) == "valid🙂"


def test_surrogate_text_is_scrubbed_before_normalization(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        scrub,
        "_scrub",
        lambda value: seen.append(value) or value.replace("sensitive", "[redacted]"),
    )
    path = tmp_path / "x.jsonl"
    path.write_text(
        json.dumps({"type": "ai-title", "sessionId": "s", "aiTitle": "sensitive\ud800"}) + "\n"
    )
    row = extract.extract_source(path, path.name).tables["session_meta"][0]
    assert "sensitive\ud800" in seen
    assert row["value"] == "[redacted]?"


@pytest.mark.parametrize(
    "flag,expected",
    [
        (None, ("agent_api_error", "text")),
        (True, ("agent_api_error", "flag")),
        (False, (None, None)),
    ],
)
def test_agent_termination_marker_respects_explicit_success(flag, expected):
    assert (
        classify(
            "Agent terminated early due to an API error: quota\npartial",
            is_error=flag,
            denial_kind=None,
        )
        == expected
    )
    assert classify(
        "log says Agent terminated early due to an API error: quota",
        is_error=None,
        denial_kind=None,
    ) == (None, None)


@pytest.mark.parametrize(
    "metadata,expected",
    [
        ({"taskKind": "in_process_teammate"}, "in_process_teammate"),
        ({"agentType": "Explore", "taskKind": "in_process_teammate"}, "Explore"),
        ({"agentType": {}, "taskKind": "in_process_teammate"}, "in_process_teammate"),
    ],
)
def test_agent_type_has_task_kind_fallback(tmp_path, metadata, expected):
    path = tmp_path / "agent-worker.jsonl"
    path.write_text("{}\n")
    path.with_suffix(".meta.json").write_text(json.dumps(metadata))
    assert extract.extract_source(path, path.name).tables["agents"][0]["agent_type"] == expected
