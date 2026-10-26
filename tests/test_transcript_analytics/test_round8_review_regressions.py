"""Regression tests for the Codex round on the A2 head.

Each test names the finding it pins.
"""

import json
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import query, store

FIXTURES = Path(__file__).with_name("fixtures")
SID = "11111111-2222-3333-4444-555555555555"
OTHER = "33333333-2222-3333-4444-555555555555"


def _asst(mid, ts, uuid, sid, tools=()):
    blocks = [
        {"type": "tool_use", "id": t, "name": "Bash", "input": {"command": f"ls-{uuid}"}}
        for t in tools
    ]
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": sid,
        "timestamp": ts,
        "message": {
            "id": mid,
            "model": "m",
            "stop_reason": "tool_use" if tools else "end_turn",
            "content": blocks,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
    }


def _res(tid, ts, sid, uuid, content="ok"):
    return {
        "type": "user",
        "uuid": uuid,
        "sessionId": sid,
        "timestamp": ts,
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]},
    }


def _write(path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))


# "Record the captured input signature in query manifests" --------------------
def test_manifest_records_captured_inputs_signature(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    _write(projects / "source.jsonl", [_asst("m1", "2026-10-01T00:00:00Z", "u1", SID)])
    assert store.ingest(projects, data)["rebuilt"] == 1
    manifest = tmp_path / "query.json"
    query.run_query(data, "SELECT 1", live=True, manifest_path=manifest)
    body = json.loads(manifest.read_text())
    selection = store.select_sources(data)
    assert body["inputs_signature"] == store.inputs_signature(data, selection=selection)
    _write(projects / "source.jsonl", [_asst("m1", "2026-10-01T00:00:00Z", "u1", SID), _asst("m2", "2026-10-01T00:01:00Z", "u2", SID)])
    assert store.ingest(projects, data)["rebuilt"] == 1
    query.run_query(data, "SELECT 1", live=True, manifest_path=manifest)
    refreshed = json.loads(manifest.read_text())
    assert refreshed["inputs_signature"] != body["inputs_signature"]


# "Scope tool IDs before deduplicating calls" ---------------------------------
def test_tool_use_id_collision_across_sources_keeps_both_calls(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    # Two independent transcripts reuse the opaque id 'dup' for unrelated calls.
    _write(
        projects / f"{SID}.jsonl",
        [
            _asst("ma", "2026-10-01T00:00:00Z", "ua", SID, tools=["dup"]),
            _res("dup", "2026-10-01T00:00:01Z", SID, "ra", content="result-A"),
        ],
    )
    _write(
        projects / f"{OTHER}.jsonl",
        [
            _asst("mb", "2026-10-01T00:00:00Z", "ub", OTHER, tools=["dup"]),
            _res("dup", "2026-10-01T00:00:01Z", OTHER, "rb", content="result-B"),
        ],
    )
    assert store.ingest(projects, data)["rebuilt"] == 2
    with query.connect(data) as con:
        rows = con.execute(
            "SELECT source_file,message_id,has_result,result_len,session_id FROM tool_calls "
            "WHERE tool_use_id='dup' ORDER BY message_id"
        ).fetchall()
    assert rows == [
        (f"{SID}.jsonl", "ma", True, len("result-A"), SID),
        (f"{OTHER}.jsonl", "mb", True, len("result-B"), OTHER),
    ]


# "Reject non-sticky writable spill roots" ------------------------------------
def test_writable_non_sticky_spill_root_is_rejected(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    root.chmod(0o777)
    monkeypatch.setenv("TA_DUCKDB_TMP", str(root))
    with pytest.raises(ValueError, match="sticky"):
        query.connect(tmp_path / "data")


def _flat_source(tmp_path, table, rows):
    data = tmp_path / "data"
    shutil.copytree(FIXTURES / "flat-v1", data, symlinks=True)
    target = next(data.glob(f"{table}__*.parquet"))
    schema = pq.read_table(target).schema
    pq.write_table(
        pa.Table.from_pylist(rows, schema=schema),
        target,
    )
    return data


# "Keep missing historical hashes out of content coverage" --------------------
def test_flat_v1_hook_event_hash_unknown_is_not_verified_content(tmp_path):
    hook = {name: None for name in pq.read_schema(FIXTURES / "flat-v1" / "hooks__654b8297f331da22.parquet").names}
    hook.update(
        {
            "source_file": "session.jsonl", "session_id": "ghost", "line_no": 1,
            "uuid": "h1", "ts": "2020-01-01T00:00:00Z", "kind": "hook_success",
            "hook_event": "PreToolUse", "hook_name": "x",
        }
    )
    event = {name: None for name in pq.read_schema(FIXTURES / "flat-v1" / "events__654b8297f331da22.parquet").names}
    event.update(
        {
            "source_file": "session.jsonl", "session_id": "ghost", "line_no": 2,
            "uuid": "e1", "ts": "2020-01-01T00:00:01Z", "subtype": "system",
        }
    )
    data = tmp_path / "data"
    shutil.copytree(FIXTURES / "flat-v1", data, symlinks=True)
    for table, rows in (("hooks", [hook]), ("events", [event])):
        target = next(data.glob(f"{table}__*.parquet"))
        pq.write_table(pa.Table.from_pylist(rows, schema=pq.read_table(target).schema), target)
    with query.connect(data) as con:
        assert con.execute("SELECT content_conflict FROM hooks").fetchall() == [(None,)]
        assert con.execute("SELECT content_conflict FROM events").fetchall() == [(None,)]
        coverage = dict(
            con.execute("SELECT metric,included FROM metric_coverage").fetchall()
        )
        assert coverage["hook_content"] == 0
        assert coverage["event_content"] == 0


# "Preserve historical agents with unknown actor identities" ------------------
def test_flat_v1_agents_with_unknown_actors_are_not_merged(tmp_path):
    names = pq.read_schema(FIXTURES / "flat-v1" / "agents__654b8297f331da22.parquet").names
    agents = []
    for agent_id in ("agent-one", "agent-two"):
        row = {name: None for name in names}
        row.update(
            {
                "source_file": "session.jsonl", "agent_id": agent_id,
                "session_id": "ghost", "agent_type": "worker",
                "description": f"desc-{agent_id}",
            }
        )
        agents.append(row)
    data = tmp_path / "data"
    shutil.copytree(FIXTURES / "flat-v1", data, symlinks=True)
    target = next(data.glob("agents__*.parquet"))
    pq.write_table(pa.Table.from_pylist(agents, schema=pq.read_table(target).schema), target)
    with query.connect(data) as con:
        assert con.execute("SELECT agent_id FROM agents ORDER BY agent_id").fetchall() == [
            ("agent-one",),
            ("agent-two",),
        ]


# "Include sessions that have no assistant turns" ------------------------------
def test_session_with_only_meta_and_hooks_appears_in_sessions(tmp_path):
    names = pq.read_schema(FIXTURES / "flat-v1" / "session_meta__654b8297f331da22.parquet").names
    meta = {name: None for name in names}
    meta.update(
        {
            "source_file": "session.jsonl", "line_no": 1,
            "session_id": "ghost", "kind": "ai-title", "value": "Ghost",
            "ts": "2020-01-01T00:00:00Z",
        }
    )
    data = tmp_path / "data"
    shutil.copytree(FIXTURES / "flat-v1", data, symlinks=True)
    target = next(data.glob("session_meta__*.parquet"))
    pq.write_table(pa.Table.from_pylist([meta], schema=pq.read_table(target).schema), target)
    with query.connect(data) as con:
        assert con.execute(
            "SELECT session_id,n_turns,ai_title FROM sessions WHERE session_id='ghost'"
        ).fetchall() == [("ghost", 0, "Ghost")]


# "Query evidence leaks raw source metadata" ----------------------------------
def test_manifest_inventory_carries_identities_not_raw_names(tmp_path):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    _write(projects / "source.jsonl", [_asst("m1", "2026-10-01T00:00:00Z", "u1", SID)])
    store.ingest(projects, data)
    # Simulate an inventory carrying an absolute root and a raw failed name.
    inventory = json.loads((data / "inventory.json").read_text())
    inventory["projects_root"] = str(projects.resolve())
    inventory["failed_sources"] = ["private-name.jsonl"]
    (data / "inventory.json").write_text(json.dumps(inventory))
    manifest = tmp_path / "query.json"
    query.run_query(data, "SELECT 1", live=True, manifest_path=manifest)
    body = json.loads(manifest.read_text())
    # _scrub_manifest_json redacts roots and hashes source names before
    # publication; the raw name and root must not survive anywhere.
    assert body["inventory"]["projects_root"] == "[redacted]"
    assert "private-name.jsonl" not in json.dumps(body)
    assert str(projects.resolve()) not in json.dumps(body)
    assert body["inventory"]["failed_sources"] == [
        store.srckey("private-name.jsonl")
    ]
