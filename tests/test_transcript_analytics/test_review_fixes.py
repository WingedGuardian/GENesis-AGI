"""Regression tests for the adversarial review of 2026-10-04 (SF-1..SF-8, N-2, N-8).
Each test names the finding it pins."""

import fcntl
import json
import os

import pytest

from genesis.transcript_analytics import scrub, store
from genesis.transcript_analytics.classify import parse_hook_block
from genesis.transcript_analytics.extract import extract_source
from genesis.transcript_analytics.query import connect

SID = "11111111-2222-3333-4444-555555555555"
TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # pragma: allowlist secret — synthetic fixture


def _asst(mid, ts, out, stop=None, uuid=None, sid=SID, tools=()):
    blocks = [
        {"type": "tool_use", "id": t, "name": "Bash", "input": {"command": "ls"}} for t in tools
    ] or [{"type": "text", "text": "x"}]
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


def _res(tid, ts, content="ok", is_error=None, sid=SID, uuid=None):
    b = {"type": "tool_result", "tool_use_id": tid, "content": content}
    if is_error is not None:
        b["is_error"] = is_error
    return {
        "type": "user",
        "uuid": uuid or f"r-{tid}-{sid}",
        "sessionId": sid,
        "timestamp": ts,
        "message": {"role": "user", "content": [b]},
    }


def _write(path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))


@pytest.fixture
def env(tmp_path):
    return tmp_path / "projects", tmp_path / "data", tmp_path / "ta.lock"


# SF-1 -------------------------------------------------------------------------
def test_sf1_hook_script_never_carries_an_inline_secret(tmp_path):
    text = f"PreToolUse:Bash hook error: [GH_TOKEN={TOKEN} /x/hooks/guard.sh --x]: blocked"
    assert parse_hook_block(text)["hook_script"] == "guard.sh"
    f = tmp_path / "p" / f"{SID}.jsonl"
    _write(
        f,
        [
            _asst("m1", "2026-10-01T00:00:00Z", 1, stop="tool_use", tools=["t1"]),
            _res("t1", "2026-10-01T00:00:01Z", content=text, is_error=True),
        ],
    )
    (tc,) = extract_source(f, "p").tables["tool_calls"]
    assert TOKEN not in json.dumps(tc)
    assert tc["hook_script"] == "guard.sh"


# SF-2 / SF-8 --------------------------------------------------------------------
def test_sf8_scrubber_missing_stores_no_text_and_flags(tmp_path, monkeypatch):
    monkeypatch.setattr(scrub, "_scrub", None)
    f = tmp_path / "p" / f"{SID}.jsonl"
    _write(
        f,
        [
            _asst("m1", "2026-10-01T00:00:00Z", 1, stop="tool_use", tools=["t1"]),
            _res("t1", "2026-10-01T00:00:01Z", content="Exit code 1\nsecret here", is_error=True),
        ],
    )
    (tc,) = extract_source(f, "p").tables["tool_calls"]
    assert tc["command"] is None and tc["error_text"] is None and tc["scrub_failed"] is True


def test_sf8_scrubber_placeholder_stores_no_text(tmp_path, monkeypatch):
    monkeypatch.setattr(scrub, "_scrub", lambda t: "[scrub-error: content withheld]")
    f = tmp_path / "p" / f"{SID}.jsonl"
    _write(
        f,
        [
            _asst("m1", "2026-10-01T00:00:00Z", 1, stop="tool_use", tools=["t1"]),
            _res("t1", "2026-10-01T00:00:01Z", content="Exit code 1\nx", is_error=True),
        ],
    )
    (tc,) = extract_source(f, "p").tables["tool_calls"]
    assert tc["error_text"] is None and tc["scrub_failed"] is True


def test_sf2_ingest_refuses_to_write_when_scrubber_unavailable(env, monkeypatch):
    projects, data, lock = env
    _write(
        projects / "p" / f"{SID}.jsonl", [_asst("m1", "2026-10-01T00:00:00Z", 1, stop="end_turn")]
    )
    monkeypatch.setattr(scrub, "_scrub", None)
    with pytest.raises(store.ScrubberUnavailable):
        store.ingest(projects, data, lock_path=lock)
    assert not list(data.glob("*.parquet")) if data.exists() else True


def test_sf2_scrubber_version_change_rebuilds(env, monkeypatch):
    projects, data, lock = env
    _write(
        projects / "p" / f"{SID}.jsonl", [_asst("m1", "2026-10-01T00:00:00Z", 1, stop="end_turn")]
    )
    assert store.ingest(projects, data, lock_path=lock)["rebuilt"] == 1
    assert store.ingest(projects, data, lock_path=lock)["rebuilt"] == 0
    monkeypatch.setattr(scrub, "version", lambda: "different0000")
    assert store.ingest(projects, data, lock_path=lock)["rebuilt"] == 1


# SF-3 -------------------------------------------------------------------------
def test_sf3_ingest_and_prune_take_the_lock(env):
    projects, data, lock = env
    _write(
        projects / "p" / f"{SID}.jsonl", [_asst("m1", "2026-10-01T00:00:00Z", 1, stop="end_turn")]
    )
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(store.Busy):
            store.ingest(projects, data, lock_path=lock)
        with pytest.raises(store.Busy):
            store.prune(data, before="2026-01-01", projects=projects, lock_path=lock)


# SF-4 -------------------------------------------------------------------------
def test_sf4_one_bad_source_does_not_stop_the_others(env):
    projects, data, lock = env
    bad = "22222222-2222-3333-4444-555555555555"
    # 2**63 overflows int64 and \ud800 is a lone surrogate: both used to abort the run.
    _write(
        projects / "p" / f"{bad}.jsonl",
        [
            {
                "type": "assistant",
                "uuid": "b\ud800",
                "sessionId": bad,
                "timestamp": "2026-10-01T00:00:00Z",
                "message": {
                    "id": "mb",
                    "model": "m",
                    "stop_reason": "end_turn",
                    "content": [],
                    "usage": {"output_tokens": 2**63},
                },
            }
        ],
    )
    _write(
        projects / "p" / f"{SID}.jsonl", [_asst("m1", "2026-10-01T00:00:00Z", 1, stop="end_turn")]
    )
    s = store.ingest(projects, data, lock_path=lock)
    assert s["failed"] == 0 and s["rebuilt"] == 2  # sanitized, not dropped
    rows = dict(connect(data).sql("select message_id, output_tokens from turns").fetchall())
    assert rows["m1"] == 1 and rows["mb"] is None  # out-of-range int stored as NULL


def test_sf4_unexpected_failure_is_counted_and_others_still_build(env, monkeypatch):
    projects, data, lock = env
    _write(
        projects / "p" / "aaaa.jsonl",
        [_asst("m0", "2026-10-01T00:00:00Z", 1, stop="end_turn", sid="a")],
    )
    _write(
        projects / "p" / f"{SID}.jsonl", [_asst("m1", "2026-10-01T00:00:00Z", 1, stop="end_turn")]
    )
    real = store.build_source

    def boom(path, rel, data_, st, **kwargs):
        if rel.endswith("aaaa.jsonl"):
            raise RuntimeError("synthetic failure")
        return real(path, rel, data_, st, **kwargs)

    monkeypatch.setattr(store, "build_source", boom)
    s = store.ingest(projects, data, lock_path=lock)
    assert s["failed"] == 1 and s["rebuilt"] == 1 and s["failed_sources"] == ["p/aaaa.jsonl"]


# SF-5 -------------------------------------------------------------------------
def test_sf5_duplicate_fragment_in_one_file_is_counted_once(env):
    projects, data, lock = env
    frag = _asst("m1", "2026-10-01T00:00:00Z", 5, stop="tool_use", uuid="dup", tools=["t1"])
    _write(projects / "p" / f"{SID}.jsonl", [frag, frag, _res("t1", "2026-10-01T00:00:01Z")])
    store.ingest(projects, data, lock_path=lock)
    assert connect(data).sql(
        "select n_tool_use, n_records from turns where message_id='m1'"
    ).fetchone() == (1, 1)


# SF-6 -------------------------------------------------------------------------
def test_sf6_tool_call_copies_follow_the_turn_attribution(env):
    projects, data, lock = env
    other = "00000000-2222-3333-4444-555555555555"  # sorts first by path
    _write(
        projects / "p" / f"{SID}.jsonl",
        [
            _asst("m0", "2026-10-01T00:00:00Z", 1, stop="end_turn", uuid="u0"),
            _asst("mX", "2026-10-01T00:01:00Z", 9, stop="tool_use", uuid="u1", tools=["tX"]),
            _res("tX", "2026-10-01T00:01:01Z", uuid="r1"),
            _asst("m1", "2026-10-01T00:05:00Z", 1, stop="end_turn", uuid="u2"),
        ],
    )
    _write(
        projects / "p" / f"{other}.jsonl",
        [
            _asst(
                "mX", "2026-10-01T00:01:00Z", 9, stop="tool_use", uuid="u1", sid=other, tools=["tX"]
            ),
            _res("tX", "2026-10-01T00:01:01Z", sid=other, uuid="r1"),
            _asst("m0", "2026-10-01T00:00:00Z", 1, stop="end_turn", uuid="u0", sid=other),
            _asst("m9", "2026-10-01T09:00:00Z", 1, stop="end_turn", uuid="u9", sid=other),
        ],
    )
    store.ingest(projects, data, lock_path=lock)
    c = connect(data)
    assert c.sql("select session_id from tool_calls where tool_use_id='tX'").fetchone() == (SID,)
    assert c.sql(
        "select count(*) from tool_calls tc join turns t using (message_id) "
        "where tc.session_id is distinct from t.session_id"
    ).fetchone() == (0,)


# SF-7 -------------------------------------------------------------------------
def test_sf7_prune_refuses_bad_date_and_missing_projects(env):
    projects, data, lock = env
    _write(
        projects / "p" / f"{SID}.jsonl", [_asst("m1", "2025-01-01T00:00:00Z", 1, stop="end_turn")]
    )
    store.ingest(projects, data, lock_path=lock)
    with pytest.raises(ValueError):
        store.prune(data, before="9", projects=projects, lock_path=lock)
    with pytest.raises(ValueError):
        store.prune(data, before="2026-01-01", projects=projects.parent / "typo", lock_path=lock)
    assert connect(data).sql("select count(*) from turns").fetchone() == (1,)


# N-2 --------------------------------------------------------------------------
def test_n2_usage_comes_from_the_last_fragment_even_when_it_is_null(env):
    projects, data, lock = env
    last = _asst("mN", "2026-10-01T00:00:01Z", 0, stop="end_turn", uuid="f2")
    last["message"]["usage"] = {"input_tokens": 1}  # output_tokens absent on the final fragment
    _write(
        projects / "p" / f"{SID}.jsonl", [_asst("mN", "2026-10-01T00:00:00Z", 7, uuid="f1"), last]
    )
    store.ingest(projects, data, lock_path=lock)
    assert connect(data).sql(
        "select output_tokens from turns where message_id='mN'"
    ).fetchone() == (None,)


# N-3 / N-8 --------------------------------------------------------------------
def test_n3_stale_staging_files_are_swept_and_nothing_is_left(env):
    projects, data, lock = env
    (data / ".staging").mkdir(parents=True)
    old = data / ".staging" / "fragments__dead.999.tmp"
    old.write_bytes(b"x")
    os.utime(old, (1, 1))
    _write(
        projects / "p" / f"{SID}.jsonl", [_asst("m1", "2026-10-01T00:00:00Z", 1, stop="end_turn")]
    )
    store.ingest(projects, data, lock_path=lock)
    assert list((data / ".staging").iterdir()) == []


def test_n8_hooks_view_dedupes_across_files(env):
    projects, data, lock = env
    hook = {
        "type": "attachment",
        "uuid": "h1",
        "sessionId": SID,
        "timestamp": "2026-10-01T00:00:00Z",
        "attachment": {
            "type": "hook_success",
            "hookEvent": "PreToolUse",
            "hookName": "x",
            "durationMs": 5,
        },
    }
    _write(projects / "p" / f"{SID}.jsonl", [hook])
    _write(projects / "p" / "00000000-2222-3333-4444-555555555555.jsonl", [hook])
    store.ingest(projects, data, lock_path=lock)
    c = connect(data)
    assert c.sql("select count(*) from raw_hooks").fetchone() == (2,)
    assert c.sql("select count(*) from hooks").fetchone() == (1,)


# Residual found on real data after SF-5/SF-6 (36 messages) -----------------------
def test_forked_child_copies_attribute_to_the_parent_holding_the_whole_message(env):
    projects, data, lock = env
    parent = projects / "p" / SID / "subagents" / "agent-parent.jsonl"
    # Parent message mF has three fragments, each spawning one child (Agent tool use).
    frags = [
        _asst("mF", f"2026-10-01T00:00:0{i}Z", 1, stop="tool_use", uuid=f"pf{i}", tools=[f"tA{i}"])
        for i in range(3)
    ]
    results = [_res(f"tA{i}", "2026-10-01T00:10:00Z", uuid=f"pr{i}") for i in range(3)]
    _write(
        parent,
        frags + results + [_asst("mEnd", "2026-10-01T00:20:00Z", 1, stop="end_turn", uuid="pend")],
    )
    # Each child file starts with a COPY of the one fragment that spawned it, and ends earlier.
    for i in range(3):
        child = projects / "p" / SID / "subagents" / f"agent-child{i}.jsonl"
        _write(
            child,
            [frags[i], _asst(f"mc{i}", "2026-10-01T00:05:00Z", 1, stop="end_turn", uuid=f"c{i}")],
        )
    store.ingest(projects, data, lock_path=lock)
    c = connect(data)
    turn = c.sql(
        "select source_file, n_tool_use, n_records from turns where message_id='mF'"
    ).fetchone()
    assert turn == (f"p/{SID}/subagents/agent-parent.jsonl", 3, 3)
    calls = c.sql(
        "select count(*), count(distinct source_file) from tool_calls where message_id='mF'"
    ).fetchone()
    assert calls == (3, 1)


def test_tool_call_is_assembled_from_its_call_row_and_a_result_row_in_another_file(env):
    projects, data, lock = env
    other = "33333333-2222-3333-4444-555555555555"
    # Original file: the call, but its result was never written there.
    _write(
        projects / "p" / f"{SID}.jsonl",
        [_asst("mO", "2026-10-01T00:00:00Z", 1, stop="tool_use", uuid="o1", tools=["tO"])],
    )
    # Another file holds only the result (an orphan there).
    _write(
        projects / "p" / f"{other}.jsonl",
        [_res("tO", "2026-10-01T00:00:02Z", content="Exit code 3\nboom", is_error=True, sid=other)],
    )
    store.ingest(projects, data, lock_path=lock)
    row = (
        connect(data)
        .sql(
            "select message_id, tool, is_error, error_class, exit_code, session_id, latency_ms "
            "from tool_calls where tool_use_id='tO'"
        )
        .fetchone()
    )
    assert row == ("mO", "Bash", True, "exit_nonzero", 3, SID, 2000)
