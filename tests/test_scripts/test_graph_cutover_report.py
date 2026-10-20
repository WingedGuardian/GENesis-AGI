"""scripts/graph_cutover_report.py: reads the durable rows read-only and exits
with a code a caller can trust (never NOT_YET's code on a failure)."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent.parent / "scripts" / "graph_cutover_report.py"
_spec = importlib.util.spec_from_file_location("graph_cutover_report", _SCRIPT)
_rep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_rep)

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _ts(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _db(
    tmp_path: Path, *, days: int, fallback_at: datetime | None = None, event: dict | None = None
) -> Path:
    path = tmp_path / "genesis.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE eval_events (id TEXT PRIMARY KEY, timestamp TEXT, dimension TEXT,"
        " event_type TEXT, subject_id TEXT, session_id TEXT, metrics_json TEXT,"
        " created_at TEXT, prompt_hash TEXT)"
    )
    census = {
        "v": 1,
        "procs": [],
        "other_servers": {},
        "unclassified": 0,
        "head_telemetry": True,
        "head_dirty": False,
        "complete": True,
        "reasons": [],
    }
    n = 0
    t = NOW - timedelta(days=days)
    while t <= NOW:
        n += 1
        conn.execute(
            "INSERT INTO eval_events (id, timestamp, dimension, event_type, metrics_json)"
            " VALUES (?, ?, 'system', 'graph_traverse_census', ?)",
            (f"c{n}", _ts(t), json.dumps(census)),
        )
        t += timedelta(hours=1)
    for d in range(14):
        row = {
            "caller": "recall",
            "proc": "mcp-memory",
            "traversals": 10,
            "outcomes": {"primary": 10},
            "served": {"falkordb": 10},
            "configured": {"falkordb": 10},
            "events": [],
            "prior_write_failures": 0,
        }
        conn.execute(
            "INSERT INTO eval_events (id, timestamp, dimension, event_type, metrics_json)"
            " VALUES (?, ?, 'system', 'graph_traverse', ?)",
            (f"t{d}", _ts(NOW - timedelta(days=d, hours=1)), json.dumps(row)),
        )
    if fallback_at is not None:
        bad = {
            "caller": "expand",
            "proc": "mcp-memory",
            "traversals": 1,
            "outcomes": {"fallback": 1},
            "served": {"networkx": 1},
            "configured": {"falkordb": 1},
            "events": [event or {"outcome": "fallback", "primary_reason": "ConnectionError"}],
            "prior_write_failures": 0,
        }
        conn.execute(
            "INSERT INTO eval_events (id, timestamp, dimension, event_type, metrics_json)"
            " VALUES ('bad', ?, 'system', 'graph_traverse', ?)",
            (_ts(fallback_at), json.dumps(bad)),
        )
    conn.commit()
    conn.close()
    return path


def _run(tmp_path: Path, db: Path, *extra: str) -> int:
    lost = tmp_path / "lost.jsonl"
    return _rep.main(["--db", str(db), "--lost-writes", str(lost), "--now", NOW.isoformat(), *extra])


def test_pass_exits_zero_and_prints_the_verdict(tmp_path, capsys):
    db = _db(tmp_path, days=15)
    assert _run(tmp_path, db) == 0
    out = capsys.readouterr().out
    assert out.startswith("FalkorDB default-on cutover: PASS")
    assert "140 served by falkordb" in out


def test_a_fallback_restarts_the_clock_and_names_its_process(tmp_path, capsys):
    db = _db(tmp_path, days=20, fallback_at=NOW - timedelta(days=2))
    assert _run(tmp_path, db, "--json") == 1
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "NOT_YET"
    assert data["resets"][-1]["caller"] == "expand"
    assert data["resets"][-1]["events"][0]["primary_reason"] == "ConnectionError"


def test_a_lost_writes_line_makes_it_inconclusive(tmp_path, capsys):
    db = _db(tmp_path, days=15)
    (tmp_path / "lost.jsonl").write_text(
        json.dumps({"ts": _ts(NOW - timedelta(days=1)), "caller": "recall"}) + "\n"
    )
    assert _run(tmp_path, db) == 2
    assert "failed to write" in capsys.readouterr().out


def test_the_database_is_opened_read_only(tmp_path, monkeypatch):
    db = _db(tmp_path, days=15)
    opened: list[tuple] = []
    real = _rep.sqlite3.connect

    def _spy(*args, **kwargs):
        opened.append((args, kwargs))
        return real(*args, **kwargs)

    monkeypatch.setattr(_rep.sqlite3, "connect", _spy)
    assert len(_rep.read_rows(db, "graph_traverse")) == 14
    [(args, kwargs)] = opened
    assert args[0] == f"{db.resolve().as_uri()}?mode=ro"
    assert kwargs["uri"] is True


def test_every_row_is_read_with_no_limit(tmp_path):
    db = _db(tmp_path, days=60)  # 1,441 census rows
    assert len(_rep.read_rows(db, "graph_traverse_census")) == 60 * 24 + 1


def test_failures_and_usage_errors_never_exit_with_not_yets_code(tmp_path):
    missing = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--db",
            str(tmp_path / "absent.db"),
            "--lost-writes",
            str(tmp_path / "lost.jsonl"),
        ],
        capture_output=True,
        text=True,
    )
    assert missing.returncode == 3
    assert "graph_cutover_report failed" in missing.stderr
    bad_arg = subprocess.run(
        [sys.executable, str(_SCRIPT), "--now", "not-a-time"], capture_output=True, text=True
    )
    assert bad_arg.returncode == 3


def test_a_database_path_with_uri_characters_still_opens(tmp_path):
    odd = tmp_path / "a?b#c%d"
    odd.mkdir()
    db = _db(odd, days=15)
    assert len(_rep.read_rows(db, "graph_traverse")) == 14


def test_another_installs_database_needs_its_own_lost_writes_file(tmp_path):
    db = _db(tmp_path, days=15)
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), "--db", str(db)], capture_output=True, text=True
    )
    assert r.returncode == 3
    assert "--db needs --lost-writes" in r.stderr



def test_a_lost_writes_file_without_its_database_is_refused(tmp_path):
    lost = tmp_path / "lost.jsonl"
    lost.write_text("")
    r = subprocess.run(
        [sys.executable, str(_SCRIPT), "--lost-writes", str(lost)], capture_output=True, text=True
    )
    assert r.returncode == 3
    assert "--lost-writes needs --db" in r.stderr


def test_the_lost_writes_file_is_read_under_a_shared_lock(tmp_path, monkeypatch):
    import fcntl

    lost = tmp_path / "lost.jsonl"
    lost.write_text('{"ts": "2026-10-31T00:00:00.000000Z"}\n')
    seen: list[int] = []
    real = fcntl.flock

    def spy(fd, op):
        seen.append(op)
        return real(fd, op)

    monkeypatch.setattr(fcntl, "flock", spy)
    assert _rep.read_lost_writes(lost) == ['{"ts": "2026-10-31T00:00:00.000000Z"}']
    assert fcntl.LOCK_SH in seen


def test_the_read_waits_for_an_exclusive_writer(tmp_path):
    import fcntl
    import threading

    lost = tmp_path / "lost.jsonl"
    lost.write_text("old\n")
    result: list[list[str]] = []
    with lost.open("r+", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        reader = threading.Thread(target=lambda: result.append(_rep.read_lost_writes(lost)))
        reader.start()
        reader.join(0.5)
        assert reader.is_alive(), "the read did not wait for the writer's lock"
        fh.seek(0)
        fh.write("new\n")
        fh.truncate()
        fh.flush()
    reader.join(10)
    assert result == [["new"]]


def test_a_missing_lost_writes_file_reads_as_empty(tmp_path):
    assert _rep.read_lost_writes(tmp_path / "absent.jsonl") == []


def test_a_reset_prints_the_cause_its_event_recorded(tmp_path, capsys):
    event = {
        "outcome": "fallback",
        "primary_reason": "TimeoutError",
        "pid": 4242,
        "starttime_ticks": 99,
        "proc_age_s": 12.5,
        "diag": {
            "budget_ms": 500.0,
            "elapsed_ms": 501.2,
            "late_ms": 1.2,
            "frames": ["query", "readuntil", "_wait_for_data"],
            "psi_stall_us": {"cpu_some": 0, "io_full": 3000},
        },
    }
    db = _db(tmp_path, days=20, fallback_at=NOW - timedelta(days=2), event=event)
    assert _run(tmp_path, db) == 1
    out = capsys.readouterr().out
    assert "pid 4242, age 12.5s" in out
    assert "late 1.2ms" in out and "budget 500.0ms" in out
    assert "at query>readuntil>_wait_for_data" in out
    assert "stall io_full=3000us" in out
    assert "cpu_some" not in out  # a zero stall is not news


def test_a_reset_written_before_the_diagnostics_says_so(tmp_path, capsys):
    db = _db(tmp_path, days=20, fallback_at=NOW - timedelta(days=2))
    assert _run(tmp_path, db) == 1
    assert "uninstrumented (written before timeout diagnostics)" in capsys.readouterr().out


def test_a_malformed_event_never_crashes_the_report(tmp_path, capsys):
    event = {"outcome": "fallback", "pid": 7, "diag": {"frames": "not-a-list", "psi_stall_us": 3}}
    db = _db(tmp_path, days=20, fallback_at=NOW - timedelta(days=2), event=event)
    assert _run(tmp_path, db) == 1
    assert "pid 7" in capsys.readouterr().out


def test_the_query_a_timeout_hit_is_named(tmp_path, capsys):
    event = {"outcome": "fallback", "pid": 7, "diag": {"query": "marker", "budget_ms": 120.0}}
    db = _db(tmp_path, days=20, fallback_at=NOW - timedelta(days=2), event=event)
    assert _run(tmp_path, db) == 1
    assert "marker query" in capsys.readouterr().out
