"""Every emitted integer respects Arrow's signed int64 schema boundary."""

import json

import pyarrow as pa
import pytest

from genesis.transcript_analytics.extract import _int, extract_source
from genesis.transcript_analytics.schema import SCHEMAS


@pytest.mark.parametrize("value", [-(2**63) - 1, -(2**63), 2**63 - 1, 2**63])
def test_integer_guard_boundaries_are_arrow_convertible(value):
    expected = value if -(2**63) <= value < 2**63 else None
    assert _int(value) == expected
    assert pa.array([_int(value)], type=pa.int64()).to_pylist() == [expected]


@pytest.mark.parametrize("value", [2**63 - 1, 2**63, 10**100, -(2**63), -(2**63) - 1])
@pytest.mark.parametrize("duplicate", [False, True])
def test_parsed_exit_codes_and_duplicate_pending_copies_convert(tmp_path, value, duplicate):
    blocks = [{"type": "tool_use", "id": "tool", "name": "Bash", "input": {"command": "echo ok"}}]
    if duplicate:
        blocks *= 2
    records = [
        {
            "type": "assistant",
            "uuid": "call",
            "sessionId": "session",
            "message": {"id": "message", "content": blocks},
        },
        {
            "type": "user",
            "uuid": "result",
            "sessionId": "session",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool",
                        "is_error": True,
                        "content": f"Exit code {value}\nnormal diagnostic",
                    }
                ]
            },
        },
    ]
    path = tmp_path / "record.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    extracted = extract_source(path, "project/record.jsonl")
    calls = extracted.tables["tool_calls"]
    # Extraction retains physical duplicates; reconciliation handles logical identity.
    assert len(calls) == (2 if duplicate else 1)  # retain every physical call observation
    expected = value if 0 <= value < 2**63 else None
    assert all(row["exit_code"] == expected for row in calls)
    for table, rows in extracted.tables.items():
        pa.Table.from_pylist(rows, schema=SCHEMAS[table])
