"""Store tests: per-source files, staleness without a state file, atomic
publish with ``fragments`` as the commit marker, and the derived views."""

import json
import os

import pyarrow.parquet as pq
import pytest

from genesis.transcript_analytics import store
from genesis.transcript_analytics.extract import TABLES
from genesis.transcript_analytics.query import connect

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


def _res(tid, ts, content="ok", is_error=None, sid=SID):
    b = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error is not None:
        b["is_error"] = is_error
    return {
        "type": "user",
        "uuid": f"r-{tid}-{ts}",
        "sessionId": sid,
        "timestamp": ts,
        "message": {"role": "user", "content": [b]},
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
        assert (data / f"{t}__{key}.parquet").exists()
    s2 = store.ingest(projects, data)
    assert s2["rebuilt"] == 0 and s2["unchanged"] == 1


def test_growth_triggers_rebuild_and_values_follow_the_file(env):
    projects, data = env
    f = projects / "p1" / f"{SID}.jsonl"
    _write(f, [_asst("m1", "2026-10-01T00:00:00Z", 1, uuid="a")])
    store.ingest(projects, data)
    with open(f, "a") as fh:  # the message's later fragment arrives in the next tick
        fh.write(
            json.dumps(_asst("m1", "2026-10-01T00:00:01Z", 305, stop="end_turn", uuid="b")) + "\n"
        )
    s = store.ingest(projects, data)
    assert s["rebuilt"] == 1
    con = connect(data)
    row = con.sql(
        "select ts, n_records, output_tokens, usage_available from turns where message_id='m1'"
    ).fetchone()
    assert row == (
        "2026-10-01T00:00:00Z",
        2,
        305,
        True,
    )  # first ts and full count survive the split


def test_crash_before_commit_marker_forces_rebuild(env, monkeypatch):
    projects, data = env
    f = projects / "p1" / f"{SID}.jsonl"
    _write(f, [_asst("m1", "2026-10-01T00:00:00Z", 5, stop="end_turn")])
    store.ingest(projects, data)
    with open(f, "a") as fh:
        fh.write(json.dumps(_asst("m2", "2026-10-01T00:00:05Z", 7, stop="end_turn")) + "\n")
    real_replace = os.replace

    # Crash on a NON-marker table: with the marker published last it is still
    # stale here; if the marker were published first it would already claim
    # the new fingerprint and the half-published source would never rebuild.
    def crash_on_marker(src, dst):
        if os.path.basename(str(dst)).startswith("agents__"):
            raise OSError("simulated crash before commit marker")
        return real_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", crash_on_marker)
    crashed = store.ingest(projects, data)  # the failure is isolated per source, not fatal
    assert crashed["failed"] == 1 and crashed["rebuilt"] == 0
    assert list((data / ".staging").iterdir()) == []  # temp files cleaned up
    monkeypatch.setattr(store.os, "replace", real_replace)
    s = store.ingest(projects, data)  # marker still old -> source is stale -> rebuilt
    assert s["rebuilt"] == 1
    assert connect(data).sql("select count(*) from turns").fetchone() == (2,)


def test_missing_table_file_forces_rebuild_and_vanished_source_is_kept(env):
    projects, data = env
    f = projects / "p1" / f"{SID}.jsonl"
    _write(f, [_asst("m1", "2026-10-01T00:00:00Z", 5, stop="end_turn")])
    store.ingest(projects, data)
    key = store.srckey("p1/" + f.name)
    (data / f"hooks__{key}.parquet").unlink()
    assert store.ingest(projects, data)["rebuilt"] == 1
    f.unlink()  # CC expired the transcript
    s = store.ingest(projects, data)
    assert s["rebuilt"] == 0 and s["sources"] == 0
    assert connect(data).sql("select count(*) from turns").fetchone() == (1,)


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
    md = pq.read_metadata(data / f"fragments__{key}.parquet").metadata
    fp = json.loads(md[b"ta.fp"])
    st = os.stat(f)
    assert fp == {
        "dev": st.st_dev,
        "ino": st.st_ino,
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
    }
    assert md[b"ta.source"].decode() == "p1/" + f.name
    assert not list((data / ".staging").glob("*"))  # nothing left behind


def test_turns_view_attributes_resumed_copies_to_earliest_file(env):
    projects, data = env
    # Name the resumed file so it sorts FIRST lexically: path order must not decide.
    other = "00000000-2222-3333-4444-555555555555"
    # Original: an earlier message, then mX (line 2); the session ends at 00:05.
    _write(
        projects / "p1" / f"{SID}.jsonl",
        [
            _asst("m0", "2026-10-01T00:00:00Z", 1, stop="end_turn", uuid="u0"),
            _asst("mX", "2026-10-01T00:01:00Z", 9, stop="end_turn", uuid="u1"),
            _asst("m1", "2026-10-01T00:05:00Z", 1, stop="end_turn", uuid="u2"),
        ],
    )
    # Resumed copy: replays m0 and mX with ORIGINAL timestamps under a new sessionId, mX at
    # line 1 here, and continues later. Same start; the resumed file ends later.
    _write(
        projects / "p1" / f"{other}.jsonl",
        [
            _asst("mX", "2026-10-01T00:01:00Z", 9, stop="end_turn", uuid="u1", sid=other),
            _asst("m0", "2026-10-01T00:00:00Z", 1, stop="end_turn", uuid="u0", sid=other),
            _asst("m9", "2026-10-01T09:00:00Z", 1, stop="end_turn", uuid="u9", sid=other),
        ],
    )
    store.ingest(projects, data)
    con = connect(data)
    rows = con.sql(
        "select session_id, n_records, output_tokens from turns where message_id='mX'"
    ).fetchall()
    assert rows == [(SID, 1, 9)]  # one turn, attributed to the original, not double-counted
    assert con.sql("select count(*) from turns").fetchone() == (4,)


def test_unfinished_message_has_no_usage(env):
    projects, data = env
    _write(
        projects / "p1" / f"{SID}.jsonl", [_asst("mU", "2026-10-01T00:00:00Z", 3)]
    )  # never a stop_reason
    store.ingest(projects, data)
    assert connect(data).sql(
        "select usage_available, output_tokens from turns where message_id='mU'"
    ).fetchone() == (
        False,
        None,
    )


def test_tool_calls_and_sessions_views(env):
    projects, data = env
    _write(
        projects / "p1" / f"{SID}.jsonl",
        [
            _asst("m1", "2026-10-01T00:00:00Z", 5, stop="tool_use", tool="toolu_1"),
            _res("toolu_1", "2026-10-01T00:00:01Z", content="Exit code 1\nno", is_error=True),
            _asst("m2", "2026-10-01T00:00:02Z", 6, stop="tool_use", tool="toolu_2"),
            _res("toolu_2", "2026-10-01T00:00:03Z", content='{"error":"nope"}'),
            {"type": "ai-title", "aiTitle": "A title", "sessionId": SID},
        ],
    )
    store.ingest(projects, data)
    con = connect(data)
    assert con.sql(
        "select count(*), count(*) filter (where error_class is not null) from tool_calls"
    ).fetchone() == (
        2,
        2,
    )
    s = con.sql(
        "select n_turns, n_tool_calls, n_errors_flagged, n_errors_text, output_tokens, ai_title "
        "from sessions where session_id=?",
        params=[SID],
    ).fetchone()
    assert s == (2, 2, 1, 1, 11, "A title")


def test_prune_removes_only_old_sources_whose_transcript_is_gone(env):
    projects, data = env
    gone = "66666666-2222-3333-4444-555555555555"
    _write(
        projects / "p1" / f"{gone}.jsonl",
        [_asst("m0", "2025-01-01T00:00:00Z", 5, stop="end_turn", sid=gone)],
    )
    _write(
        projects / "p1" / f"{SID}.jsonl", [_asst("m1", "2025-01-01T00:00:00Z", 5, stop="end_turn")]
    )
    new = "77777777-2222-3333-4444-555555555555"
    _write(
        projects / "p1" / f"{new}.jsonl",
        [_asst("m2", "2026-10-01T00:00:00Z", 5, stop="end_turn", sid=new)],
    )
    store.ingest(projects, data)
    (projects / "p1" / f"{gone}.jsonl").unlink()  # expired by CC
    # Old-but-present (m1) is kept: pruning it would only be rebuilt by the next ingest.
    assert store.prune(data, before="2026-01-01", projects=projects) == 1
    assert sorted(connect(data).sql("select message_id from turns").fetchall()) == [
        ("m1",),
        ("m2",),
    ]
    assert store.ingest(projects, data)["rebuilt"] == 0
