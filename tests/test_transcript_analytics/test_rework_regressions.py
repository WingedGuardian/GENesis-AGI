"""Rework acceptance tests.

- A writer killed mid-ingest leaves the store readable, and a fresh ingest
  finishes it (acceptance 2).
- Verify-RED for review #21: the hooks/events dedup views must keep
  ``scrub_failed`` from every copy, not only the kept row.
- Verify-RED for review #23: the fingerprint is compared whole, from the
  file descriptor the extraction actually read.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.transcript_analytics import extract, store
from genesis.transcript_analytics.query import connect

SID = "11111111-2222-3333-4444-555555555555"


def _asst(mid, ts, out, stop=None, uuid=None, sid=SID):
    return {
        "type": "assistant",
        "uuid": uuid or f"{mid}-{ts}",
        "sessionId": sid,
        "timestamp": ts,
        "message": {
            "id": mid,
            "model": "m",
            "stop_reason": stop,
            "content": [{"type": "text", "text": "x"}],
            "usage": {"input_tokens": 1, "output_tokens": out},
        },
    }


@pytest.fixture
def env(tmp_path):
    projects = tmp_path / "projects"
    data = tmp_path / "data"
    lock = tmp_path / "locks" / "ta.lock"
    (projects / "p").mkdir(parents=True)
    return projects, data, lock


def _write(path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in recs))


def _hook(uuid, command):
    return {
        "type": "attachment",
        "uuid": uuid,
        "sessionId": SID,
        "timestamp": "2026-10-01T00:00:00Z",
        "attachment": {
            "type": "hook_success",
            "hookEvent": "PreToolUse",
            "hookName": "x",
            "command": command,
            "durationMs": 5,
        },
    }


def _event(uuid, content):
    return {
        "type": "system",
        "subtype": "api_error",
        "uuid": uuid,
        "sessionId": SID,
        "timestamp": "2026-10-01T00:00:00Z",
        "content": content,
    }


# Review #21: dedup keeps row 1's columns; scrub_failed must survive any row.
def test_hooks_dedup_keeps_scrub_failed_from_dropped_copy(env, monkeypatch):
    projects, data, lock = env
    monkeypatch.setattr(
        extract, "scrub_text", lambda t: (t, bool(t) and "SECRET" in t)
    )
    # Dedup keeps the lexically first source_file: the clean copy sits there,
    # the flagged copy is the row the old code dropped.
    _write(projects / "p" / f"{SID}.jsonl", [_hook("h1", "clean")])
    _write(
        projects / "p" / "ffffffff-2222-3333-4444-555555555555.jsonl",
        [_hook("h1", "has SECRET")],
    )
    store.ingest(projects, data, lock_path=lock)
    rows = connect(data).sql("select scrub_failed from hooks").fetchall()
    assert rows == [(True,)]


def test_events_dedup_keeps_scrub_failed_from_dropped_copy(env, monkeypatch):
    projects, data, lock = env
    monkeypatch.setattr(
        extract,
        "scrub_json",
        lambda d: (json.dumps(d), "SECRET" in json.dumps(d)),
    )
    _write(projects / "p" / f"{SID}.jsonl", [_event("e1", "clean")])
    _write(
        projects / "p" / "ffffffff-2222-3333-4444-555555555555.jsonl",
        [_event("e1", "has SECRET")],
    )
    store.ingest(projects, data, lock_path=lock)
    rows = connect(data).sql("select scrub_failed from events").fetchall()
    assert rows == [(True,)]


# Review #23: fingerprint the descriptor the extraction read, whole fingerprint.
def test_source_growth_during_extraction_fails_closed(env, monkeypatch):
    projects, data, lock = env
    f = projects / "p" / f"{SID}.jsonl"
    _write(f, [_asst("m1", "2026-10-01T00:00:00Z", 1, stop="end_turn")])
    real = store.extract_source

    def grow(path, rel, stop_at=None, fh=None):
        res = real(path, rel, stop_at=stop_at, fh=fh)
        with open(path, "a") as out:
            out.write("\n")
        return res

    monkeypatch.setattr(store, "extract_source", grow)
    store.ingest(projects, data, lock_path=lock)
    # The churned source fails closed instead of pinning a stale fingerprint.
    assert store.compatible_sources(data) == ([], [])
    monkeypatch.setattr(store, "extract_source", real)
    store.ingest(projects, data, lock_path=lock)
    assert len(store.compatible_sources(data)[0]) == 1


# Acceptance 2: a writer killed mid-ingest leaves the store readable.
_KILL_SCRIPT = """
import os, sys
import pyarrow.parquet as pq
from genesis.transcript_analytics import store
real = pq.write_table
count = [0]
def killer(body, dst, **kw):
    count[0] += 1
    if count[0] == int(os.environ["TA_KILL_AT"]):
        os.kill(os.getpid(), 9)
    return real(body, dst, **kw)
pq.write_table = killer
from pathlib import Path
store.ingest(Path(sys.argv[1]), Path(sys.argv[2]), lock_path=Path(sys.argv[3]))
"""


def test_writer_killed_mid_ingest_leaves_a_clean_store(env):
    projects, data, lock = env
    names = [
        f"0000000{i}-2222-3333-4444-555555555555.jsonl" for i in range(4)
    ]
    for i, name in enumerate(names):
        _write(
            projects / "p" / name,
            [_asst(f"m{i}", "2026-10-01T00:00:00Z", 1, stop="end_turn")],
        )
    src = Path(__file__).resolve().parents[2] / "src"
    envvars = dict(os.environ, PYTHONPATH=str(src), TA_KILL_AT="8")
    proc = subprocess.run(
        [sys.executable, "-c", _KILL_SCRIPT, str(projects), str(data), str(lock)],
        env=envvars,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == -9, proc.stderr
    # Whatever the crash left, readers take no lock and read cleanly.
    c = connect(data)
    survivors = c.sql("select count(*) from raw_fragments").fetchone()[0]
    assert 0 <= survivors < 4
    # The crash left records without a projects root; adoption re-binds them.
    store.ingest(projects, data, lock_path=lock, adopt_projects_root=True)
    assert not (data / ".staging").exists()
    assert connect(data).sql("select count(*) from raw_fragments").fetchone() == (4,)
    assert len(store.compatible_sources(data)[0]) == 4
