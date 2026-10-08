"""Regression tests for the proactive hook's aggregate run deadline."""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import time
from pathlib import Path
from typing import NoReturn

import pytest

_REPO_DIR = Path(__file__).resolve().parent.parent.parent
_SCRIPTS_DIR = _REPO_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import proactive_memory_hook as hook  # noqa: E402


class _RecordingWriter:
    """Collect emitted hook lines for assertions."""

    def __init__(self) -> None:
        """Initialize an empty emission record."""
        self.lines: list[tuple[str, str]] = []

    def emit(self, text: str, *, block: str) -> None:
        """Record one emitted line and its output block."""
        self.lines.append((text, block))


@pytest.mark.asyncio
async def test_run_flushes_deferred_lines_when_recall_exceeds_total_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A slow recall cannot strand metadata when the aggregate budget expires."""
    writer = _RecordingWriter()
    db_path = tmp_path / "genesis.db"
    db_path.touch()

    async def _slow_server(*_args, **_kwargs):
        """Simulate server recall that exceeds the aggregate deadline."""
        await asyncio.sleep(0.25)
        return None, "slow recall"

    monkeypatch.setattr(hook, "_OUT", writer)
    monkeypatch.setattr(hook, "_RUN_DEADLINE_S", 0.01)
    monkeypatch.setattr(hook, "_HOOK_MODE", "server")
    monkeypatch.setattr(hook, "_DB_PATH", db_path)
    monkeypatch.setattr(hook.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(hook, "_call_server", _slow_server)
    monkeypatch.setattr(hook, "_heartbeat_write", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(hook, "_heartbeat_read_and_inject", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(hook, "_extract_keywords", lambda *_args, **_kwargs: ["deadline"])
    monkeypatch.setattr(
        hook,
        "_update_and_format_trail",
        lambda *_args, **_kwargs: "[Session trail] deferred",
    )
    monkeypatch.setattr(hook, "_extract_genesis_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, "_load_recent_files", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(hook, "_ensure_knowledge_retrieved_count", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(hook, "_compute_suppress_ids", lambda *_args, **_kwargs: frozenset())

    started = time.monotonic()
    await hook._run("why did recall stall", session_id="deadline-session")
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, "the aggregate deadline did not stop the slow recall"
    assert writer.lines == [
        ("[Session trail] deferred", "session-metadata"),
        (hook._out_of_time_notice(0.0), "recall-timeout"),
    ]
    assert (tmp_path / ".genesis" / ".knowledge_retrieved_count_migrated").exists()


@pytest.mark.asyncio
async def test_local_mode_stops_after_blocking_sync_phase_exhausts_deadline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A blocking local phase cannot make later sync phases run past the deadline."""
    writer = _RecordingWriter()
    db_path = tmp_path / "genesis.db"
    db_path.touch()
    code_calls: list[bool] = []

    def _blocking_fts(*_args, **_kwargs):
        """Simulate synchronous FTS work that overruns the deadline."""
        time.sleep(0.05)
        return []

    def _code_search(*_args, **_kwargs):
        """Record whether a later code-search phase was reached."""
        code_calls.append(True)
        time.sleep(0.2)
        return []

    monkeypatch.setattr(hook, "_OUT", writer)
    monkeypatch.setattr(hook, "_RUN_DEADLINE_S", 0.01)
    monkeypatch.setattr(hook, "_HOOK_MODE", "local")
    monkeypatch.setattr(hook, "_DB_PATH", db_path)
    monkeypatch.setattr(hook.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(hook, "_heartbeat_write", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(hook, "_heartbeat_read_and_inject", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(hook, "_extract_keywords", lambda *_args, **_kwargs: ["deadline"])
    monkeypatch.setattr(
        hook,
        "_update_and_format_trail",
        lambda *_args, **_kwargs: "[Session trail] deferred",
    )
    monkeypatch.setattr(hook, "_extract_genesis_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, "_load_recent_files", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(hook, "_ensure_knowledge_retrieved_count", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(hook, "_compute_suppress_ids", lambda *_args, **_kwargs: frozenset())
    monkeypatch.setattr(hook, "_search_fts5", _blocking_fts)
    monkeypatch.setattr(hook, "_search_code_index", _code_search)

    started = time.monotonic()
    await hook._run("why did recall stall", session_id="deadline-session")
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, "a later sync phase ran after the aggregate deadline"
    assert code_calls == []
    assert writer.lines == [
        ("[Session trail] deferred", "session-metadata"),
        (hook._out_of_time_notice(0.0), "recall-timeout"),
    ]
    assert not (tmp_path / ".genesis" / ".knowledge_retrieved_count_migrated").exists()


@pytest.mark.asyncio
async def test_failed_knowledge_migration_does_not_create_sentinel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A migration exception leaves the self-heal eligible for the next run."""
    writer = _RecordingWriter()
    db_path = tmp_path / "genesis.db"
    db_path.touch()

    def _interrupted_migration(*_args: object, **_kwargs: object) -> NoReturn:
        """Simulate SQLite interrupting the migration in the caller path."""
        raise sqlite3.OperationalError("interrupted")

    monkeypatch.setattr(hook, "_OUT", writer)
    monkeypatch.setattr(hook, "_RUN_DEADLINE_S", 1.0)
    monkeypatch.setattr(hook, "_HOOK_MODE", "local")
    monkeypatch.setattr(hook, "_DB_PATH", db_path)
    monkeypatch.setattr(hook.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(hook, "_heartbeat_write", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(hook, "_heartbeat_read_and_inject", lambda *_args, **_kwargs: 0.0)
    monkeypatch.setattr(hook, "_extract_keywords", lambda *_args, **_kwargs: ["migration"])
    monkeypatch.setattr(hook, "_update_and_format_trail", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, "_extract_genesis_summary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, "_load_recent_files", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(hook, "_ensure_knowledge_retrieved_count", _interrupted_migration)
    monkeypatch.setattr(hook, "_compute_suppress_ids", lambda *_args, **_kwargs: frozenset())
    monkeypatch.setattr(hook, "_search_fts5", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(hook, "_search_code_index", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(hook, "_record_activity", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, "_record_detail", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(hook, "_ambient_fold", lambda *_args, **_kwargs: None)

    await hook._run("retry migration", session_id="migration-session")

    assert not (tmp_path / ".genesis" / ".knowledge_retrieved_count_migrated").exists()


def test_knowledge_migration_adds_missing_column(tmp_path: Path) -> None:
    """A missing retrieved-count column is added and reported as successful."""
    db_path = tmp_path / "genesis.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE knowledge_units (id INTEGER PRIMARY KEY)")

    assert hook._ensure_knowledge_retrieved_count(db_path) is True

    with sqlite3.connect(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(knowledge_units)")}
    assert "retrieved_count" in columns


def test_knowledge_migration_accepts_duplicate_column(tmp_path: Path) -> None:
    """An already-applied migration remains an idempotent success."""
    db_path = tmp_path / "genesis.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE knowledge_units "
            "(id INTEGER PRIMARY KEY, retrieved_count INTEGER NOT NULL DEFAULT 0)"
        )

    assert hook._ensure_knowledge_retrieved_count(db_path) is True


def test_knowledge_migration_propagates_interrupted_alter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An interrupted ALTER TABLE is not mistaken for a duplicate column."""

    class _InterruptedConnection:
        """Minimal connection double that interrupts the migration statement."""

        def execute(self, _sql: str) -> None:
            """Raise the SQLite interruption reported by the progress handler."""
            raise sqlite3.OperationalError("interrupted")

        def close(self) -> None:
            """Match the connection cleanup interface."""
            pass

    monkeypatch.setattr(
        hook,
        "_sqlite_connect",
        lambda *_args, **_kwargs: _InterruptedConnection(),
    )

    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        hook._ensure_knowledge_retrieved_count(tmp_path / "genesis.db")


# ── The budget counts from process start (box-stall kills, 2026-10-07) ──────


def _quiet_run_body(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, writer: object) -> Path:
    """Stub every phase of _run_body except the ones a test overrides."""
    db_path = tmp_path / "genesis.db"
    db_path.touch()
    monkeypatch.setattr(hook, "_OUT", writer)
    monkeypatch.setattr(hook, "_HOOK_MODE", "server")
    monkeypatch.setattr(hook, "_DB_PATH", db_path)
    monkeypatch.setattr(hook.Path, "home", staticmethod(lambda: tmp_path))
    monkeypatch.setattr(hook, "_heartbeat_write", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(hook, "_heartbeat_read_and_inject", lambda *_a, **_k: 0.0)
    monkeypatch.setattr(hook, "_extract_keywords", lambda *_a, **_k: ["deadline"])
    monkeypatch.setattr(hook, "_update_and_format_trail", lambda *_a, **_k: None)
    monkeypatch.setattr(hook, "_extract_genesis_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(hook, "_load_recent_files", lambda *_a, **_k: [])
    monkeypatch.setattr(hook, "_ensure_knowledge_retrieved_count", lambda *_a, **_k: True)
    monkeypatch.setattr(hook, "_compute_suppress_ids", lambda *_a, **_k: frozenset())
    monkeypatch.setattr(hook, "_search_code_index", lambda *_a, **_k: [])
    monkeypatch.setattr(
        hook,
        "_ws_measure",
        lambda *_a, **_k: {
            "injected_ids": [],
            "repeat_count": 0,
            "overlap_pct": 0.0,
            "working_set_size": 0,
            "zero_retrieved_injected": 0,
            "procedure_repeat": False,
        },
    )
    monkeypatch.setattr(hook, "_record_activity", lambda *_a, **_k: None)
    monkeypatch.setattr(hook, "_record_detail", lambda *_a, **_k: None)
    return db_path


@pytest.mark.asyncio
async def test_time_spent_before_run_shrinks_the_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A hook that started late gets only what is left of the 8 s, and says so."""
    writer = _RecordingWriter()
    _quiet_run_body(monkeypatch, tmp_path, writer)

    async def _slow_server(*_a, **_k):
        await asyncio.sleep(0.5)
        return None, "slow recall"

    monkeypatch.setattr(hook, "_call_server", _slow_server)
    age = hook._RUN_DEADLINE_S - 0.05  # 50 ms of budget left
    started = time.monotonic()
    await hook._run("why did recall stall", session_id="late-start", process_age_s=age)
    elapsed = time.monotonic() - started

    assert elapsed < 0.4, f"the run kept a budget it no longer had ({elapsed:.2f}s)"
    assert writer.lines == [(hook._out_of_time_notice(age), "recall-timeout")]


@pytest.mark.asyncio
async def test_a_run_inside_its_budget_prints_no_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The notice is for runs that ran out of time, not for every quiet prompt."""
    writer = _RecordingWriter()
    _quiet_run_body(monkeypatch, tmp_path, writer)
    monkeypatch.setattr(hook, "_search_fts5", lambda *_a, **_k: [])

    async def _down_server(*_a, **_k):
        return None, "server down"

    monkeypatch.setattr(hook, "_call_server", _down_server)
    await hook._run("why did recall stall", session_id="on-time", process_age_s=1.0)

    assert writer.lines == []


@pytest.mark.asyncio
async def test_recall_that_landed_is_not_called_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Running out of time AFTER recall printed owes no notice."""

    class _Writer(_RecordingWriter):
        closed = False

    writer = _Writer()
    _quiet_run_body(monkeypatch, tmp_path, writer)

    async def _ok_server(*_a, **_k):
        return {"lines": ["[Memory | id:abc] recalled"], "results": []}, None

    def _slow_measure(*_a, **_k):
        time.sleep(0.1)  # bookkeeping after the flush outlives the budget
        return {
            "injected_ids": [],
            "repeat_count": 0,
            "overlap_pct": 0.0,
            "working_set_size": 0,
            "zero_retrieved_injected": 0,
            "procedure_repeat": False,
        }

    monkeypatch.setattr(hook, "_call_server", _ok_server)
    monkeypatch.setattr(hook, "_ws_measure", _slow_measure)
    await hook._run(
        "why did recall stall",
        session_id="landed",
        process_age_s=hook._RUN_DEADLINE_S - 0.05,
    )

    assert writer.lines == [("[Memory | id:abc] recalled", "server-recall")]


@pytest.mark.asyncio
async def test_spent_budget_skips_the_ambient_fold(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The fold runs after output is final, so a spent budget must skip it."""

    class _Writer(_RecordingWriter):
        closed = False

    writer = _Writer()
    _quiet_run_body(monkeypatch, tmp_path, writer)
    folds: list[bool] = []

    async def _ok_server(*_a, **_k):
        return {"lines": ["[Memory | id:abc] recalled"], "results": []}, None

    def _slow_detail(*_a, **_k):
        time.sleep(0.1)

    monkeypatch.setattr(hook, "_call_server", _ok_server)
    monkeypatch.setattr(hook, "_record_detail", _slow_detail)
    monkeypatch.setattr(hook, "_ambient_fold", lambda *_a, **_k: folds.append(True))

    await hook._run("fold me", session_id="fold", process_age_s=hook._RUN_DEADLINE_S - 0.05)
    assert folds == []

    # Control: with budget to spare the same run DOES fold.
    await hook._run("fold me", session_id="fold", process_age_s=0.0)
    assert folds == [True]


def test_spent_budget_skips_the_session_row_touch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """touch_terminal_session_row_sync takes no deadline, so the caller must check."""
    import genesis.db.crud.cc_sessions as cc_sessions
    import genesis.db.crud.session_heartbeats as heartbeats

    db_path = tmp_path / "genesis.db"
    db_path.touch()
    touches: list[str] = []
    monkeypatch.setattr(hook, "_extract_genesis_summary", lambda *_a, **_k: None)
    monkeypatch.setattr(hook, "cached_model", lambda *_a, **_k: None)
    monkeypatch.setattr(hook, "resolve_topic", lambda *_a, **_k: None)
    upserts: list[str] = []

    def _slow_upsert(*_a, cc_session_id: str, **_k) -> None:
        upserts.append(cc_session_id)
        time.sleep(0.1)  # the budget runs out between the upsert and the touch

    monkeypatch.setattr(heartbeats, "upsert_sync", _slow_upsert)
    monkeypatch.setattr(
        cc_sessions, "touch_terminal_session_row_sync", lambda _db, sid: touches.append(sid)
    )

    hook._heartbeat_write(db_path, "spent", "p", deadline=time.monotonic() + 0.05)
    assert upserts == ["spent"]  # the entry check passed: this tests the touch check
    assert touches == []
    hook._heartbeat_write(db_path, "live", "p", deadline=time.monotonic() + 60)
    assert touches == ["live"]


def test_spent_budget_skips_both_heartbeat_steps_on_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An already-spent budget does no heartbeat work at all (no file reads, no DB)."""
    db_path = tmp_path / "genesis.db"
    db_path.touch()
    calls: list[str] = []
    monkeypatch.setattr(hook, "_extract_genesis_summary", lambda *_a, **_k: calls.append("s"))
    spent = time.monotonic() - 1
    assert hook._heartbeat_write(db_path, "sid", "p", deadline=spent) == 0.0
    assert hook._heartbeat_read_and_inject(db_path, "sid", deadline=spent) == 0.0
    assert calls == []


def test_process_age_reads_this_process() -> None:
    """A fresh interpreter is a fraction of a second old on any healthy box."""
    import subprocess

    code = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "import proactive_memory_hook as h; print(h._process_age_s())"
    )
    out = (
        subprocess.run(
            [sys.executable, "-c", code, str(_SCRIPTS_DIR)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        .stdout.strip()
        .splitlines()[-1]
    )
    if out == "None":
        pytest.skip("no /proc/self/stat or CLOCK_BOOTTIME on this platform")
    age = float(out)
    import hook_deadline

    assert 0.0 <= age < hook_deadline.PROCESS_AGE_MAX_S


def test_process_age_disbelieves_a_clock_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A negative or implausibly large age reads as unknown, never as a budget.

    The clock is pinned relative to THIS process's real start time, so the test
    does not depend on how long pytest has been running.
    """
    import os

    stat = Path("/proc/self/stat")
    if not hasattr(time, "CLOCK_BOOTTIME") or not stat.exists():
        pytest.skip("no /proc/self/stat or CLOCK_BOOTTIME on this platform")
    raw = stat.read_text()
    fields = raw[raw.rindex(")") + 2 :].split()
    started = int(fields[19]) / os.sysconf("SC_CLK_TCK")

    monkeypatch.setattr(hook.time, "clock_gettime", lambda _clk: started + 5.0)
    assert hook._process_age_s() == pytest.approx(5.0)
    monkeypatch.setattr(hook.time, "clock_gettime", lambda _clk: started - 1.0)
    assert hook._process_age_s() is None
    monkeypatch.setattr(hook.time, "clock_gettime", lambda _clk: started + 3600.0)
    assert hook._process_age_s() is None


def test_main_charges_the_process_age(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() is the only caller that knows the age; it must pass it on."""
    import io
    import json

    seen: list[float] = []

    async def _capture(prompt: str, session_id: str = "", *, process_age_s: float = 0.0) -> None:
        seen.append(process_age_s)

    monkeypatch.setattr(hook, "_run", _capture)
    monkeypatch.setattr(hook, "_process_age_s", lambda: 6.5)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "hi", "session_id": "s"})))
    hook.main()
    assert seen == [6.5]

    monkeypatch.setattr(hook, "_process_age_s", lambda: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "hi", "session_id": "s"})))
    hook.main()
    assert seen == [6.5, 0.0]


# ── PR-1 revision: notice ownership, hard-stop notice, arrived answers, fsync ──


def test_hard_stop_notice_is_owed_only_before_output_is_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hook_output import HOOK_STDOUT_CAP

    class _Out:
        emitted_chars = 0

    monkeypatch.setattr(hook, "_OUT", _Out())
    monkeypatch.setattr(hook, "_PROCESS_AGE", 0.0)
    monkeypatch.setattr(hook, "_FLUSHED", False)
    monkeypatch.setattr(hook, "_RECALL_LANDED", False)
    assert hook._hard_stop_notice() == hook._out_of_time_notice(0.0)

    monkeypatch.setattr(hook, "_FLUSHED", True)
    assert hook._hard_stop_notice() is None  # output already final
    monkeypatch.setattr(hook, "_FLUSHED", False)
    monkeypatch.setattr(hook, "_RECALL_LANDED", True)
    assert hook._hard_stop_notice() is None  # recall already reached the model
    monkeypatch.setattr(hook, "_RECALL_LANDED", False)

    _Out.emitted_chars = HOOK_STDOUT_CAP - 3 - 20
    clipped = hook._hard_stop_notice()
    assert clipped is not None and len(clipped) <= 20  # stays under the harness cap
    _Out.emitted_chars = HOOK_STDOUT_CAP
    assert hook._hard_stop_notice() is None


def test_notice_mentions_startup_only_when_it_mattered() -> None:
    assert "before it started" not in hook._out_of_time_notice(0.0)
    assert "before it started" not in hook._out_of_time_notice(0.6)
    assert "(2.5s passed before it started)" in hook._out_of_time_notice(2.5)


@pytest.mark.asyncio
async def test_a_run_that_reached_its_decision_owes_no_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The deadline passing DURING the final flush is not a missing recall."""

    class _SlowWriter(_RecordingWriter):
        def emit(self, text: str, *, block: str) -> None:
            time.sleep(0.1)  # the budget runs out while the output is written
            super().emit(text, block=block)

    writer = _SlowWriter()
    _quiet_run_body(monkeypatch, tmp_path, writer)
    monkeypatch.setattr(hook, "_extract_keywords", lambda *_a, **_k: [])  # a short prompt
    monkeypatch.setattr(hook, "_update_and_format_trail", lambda *_a, **_k: "[Session trail] x")
    await hook._run("ok", session_id="short", process_age_s=hook._RUN_DEADLINE_S - 0.05)
    assert writer.lines == [("[Session trail] x", "session-metadata")]


@pytest.mark.asyncio
async def test_a_run_that_started_past_its_budget_says_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Even a would-be short prompt: the run never got far enough to know."""
    writer = _RecordingWriter()
    _quiet_run_body(monkeypatch, tmp_path, writer)
    monkeypatch.setattr(hook, "_extract_keywords", lambda *_a, **_k: [])
    age = hook._RUN_DEADLINE_S + 1
    await hook._run("ok", session_id="late", process_age_s=age)
    assert writer.lines == [(hook._out_of_time_notice(age), "recall-timeout")]


@pytest.mark.asyncio
async def test_an_answer_that_arrived_late_is_still_printed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The budget ran out while waiting; the answer it waited for still lands."""

    class _Writer(_RecordingWriter):
        closed = False

    writer = _Writer()
    _quiet_run_body(monkeypatch, tmp_path, writer)

    async def _slow_ok_server(*_a, **_k):
        time.sleep(0.1)  # synchronous: the asyncio timeout cannot cut it short
        return {"lines": ["[Memory | id:abc] recalled"], "results": []}, None

    monkeypatch.setattr(hook, "_call_server", _slow_ok_server)
    await hook._run(
        "why did recall stall",
        session_id="late-answer",
        process_age_s=hook._RUN_DEADLINE_S - 0.05,
    )
    assert ("[Memory | id:abc] recalled", "server-recall") in writer.lines
    assert all(block != "recall-timeout" for _t, block in writer.lines)


def test_hook_connections_do_not_fsync_every_commit(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    conn = hook._sqlite_connect(db, timeout=1, deadline=time.monotonic() + 30)
    try:
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    finally:
        conn.close()


def test_the_hard_stop_is_never_armed_on_import() -> None:
    """Tests and ambient_replay import this module; a timer here would os._exit them."""
    assert hook._HARD_STOP is None


def test_the_running_hook_arms_the_hard_stop() -> None:
    """Run as a script, the hook arms at 8.5 s from spawn and disarms on a normal exit."""
    import subprocess

    probe = (
        "import runpy, sys, threading;"
        "sys.argv=[sys.argv[1]];"
        "import io; sys.stdin=io.StringIO('');"
        "g = runpy.run_path(sys.argv[0], run_name='__main__');"
        "t = g['_HARD_STOP'];"
        "print(type(t).__name__, g['_HARD_STOP_S'], t.finished.is_set() or not t.is_alive())"
    )
    import os

    env = dict(os.environ)
    env.pop("GENESIS_CC_SESSION", None)  # a dispatched-session flag makes the hook exit early
    out = subprocess.run(
        [sys.executable, "-c", probe, str(_SCRIPTS_DIR / "proactive_memory_hook.py")],
        capture_output=True, text=True, timeout=60, check=True, env=env,
    ).stdout.strip().splitlines()[-1]
    assert out == "Timer 8.5 True"


@pytest.mark.asyncio
async def test_landed_recall_owes_no_notice_even_when_the_run_dies_before_flushing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Recall already printed, then a later step fails past the deadline: no notice."""

    class _Writer(_RecordingWriter):
        closed = False

    writer = _Writer()
    _quiet_run_body(monkeypatch, tmp_path, writer)

    async def _ok_server(*_a, **_k):
        return {"lines": ["[Memory | id:abc] recalled"], "results": []}, None

    def _dies_late(*_a, **_k):
        time.sleep(0.1)  # past the deadline, before the run flushes
        raise RuntimeError("code index broke")

    monkeypatch.setattr(hook, "_call_server", _ok_server)
    monkeypatch.setattr(hook, "_search_code_index", _dies_late)
    with pytest.raises(RuntimeError, match="code index broke"):
        await hook._run(
            "why did recall stall",
            session_id="landed-then-died",
            process_age_s=hook._RUN_DEADLINE_S - 0.05,
        )
    assert ("[Memory | id:abc] recalled", "server-recall") in writer.lines
    assert all(block != "recall-timeout" for _t, block in writer.lines)
