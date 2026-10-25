"""Storage-only portion of the original store suite; view assertions follow in A2."""

import json
import os

import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import store
from genesis.transcript_analytics.extract import TABLES

SID = "11111111-2222-3333-4444-555555555555"


def _asst(mid, ts, out, stop=None, uuid=None, sid=SID, tool=None):
    blocks = (
        [{"type": "tool_use", "id": tool, "name": "Bash", "input": {"command": "ls"}}]
        if tool
        else [{"type": "text", "text": "x"}]
    )
    return {
        "type": "assistant",
        "uuid": uuid or f"{mid}-{ts}",
        "sessionId": sid,
        "timestamp": ts,
        "message": {
            "id": mid,
            "model": "m",
            "stop_reason": stop,
            "content": blocks,
            "usage": {"input_tokens": 1, "output_tokens": out},
        },
    }


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects"
    data = tmp_path / "data"
    (projects / "p1").mkdir(parents=True)
    return projects, data


def _write(path, recs):
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))


def test_ingest_builds_one_file_per_table_and_skips_unchanged(env):
    projects, data = env
    f = projects / "p1" / f"{SID}.jsonl"
    _write(f, [_asst("m1", "2026-10-01T00:00:00Z", 5, stop="end_turn")])
    s1 = store.ingest(projects, data)
    assert s1["rebuilt"] == 1 and s1["unchanged"] == 0
    key = store.srckey("p1/" + f.name)
    for t in TABLES:
        assert store._table_path(data, t, key).exists()
    s2 = store.ingest(projects, data)
    assert s2["rebuilt"] == 0 and s2["unchanged"] == 1


def test_journal_and_non_jsonl_files_are_not_sources(env):
    projects, data = env
    wf = projects / "p1" / SID / "subagents" / "workflows" / "wf_x"
    wf.mkdir(parents=True)
    (wf / "journal.jsonl").write_text('{"type":"started","agentId":"a"}\n')
    (projects / "p1" / "notes.txt").write_text("x")
    _write(wf / "agent-a.jsonl", [_asst("m9", "2026-10-01T00:00:00Z", 3, stop="end_turn")])
    assert store.ingest(projects, data)["sources"] == 1


def test_fingerprint_metadata_and_staging_is_outside_globs(env):
    projects, data = env
    f = projects / "p1" / f"{SID}.jsonl"
    _write(f, [_asst("m1", "2026-10-01T00:00:00Z", 5, stop="end_turn")])
    store.ingest(projects, data)
    key = store.srckey("p1/" + f.name)
    md = pq.read_metadata(store._table_path(data, "fragments", key)).metadata
    fp = json.loads(md[b"ta.fp"])
    st = os.stat(f)
    assert fp == {
        "dev": st.st_dev,
        "ino": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }
    assert md[b"ta.source"].decode() == "p1/" + f.name
    assert not (data / ".staging").exists()
