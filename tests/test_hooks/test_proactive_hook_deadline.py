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

    def emit_final(self, text: str, fallback: str | None = None) -> None:
        """Record a closing line (the out-of-time notice is one)."""
        self.lines.append((text, "final"))


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
        (hook._out_of_time_notice(0.0), "final"),
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
        (hook._out_of_time_notice(0.0), "final"),
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
    assert writer.lines == [(hook._out_of_time_notice(age), "final")]


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

    written = {"units": 0}
    monkeypatch.setattr(hook, "written_units", lambda: written["units"])
    monkeypatch.setattr(hook, "_PROCESS_AGE", 0.0)
    monkeypatch.setattr(hook, "_HOOK_MODE", "server")
    monkeypatch.setattr(hook, "_FLUSHED", False)
    monkeypatch.setattr(hook, "_RECALL_LANDED", False)
    assert hook._hard_stop_notice() == hook._out_of_time_notice(0.0)

    monkeypatch.setattr(hook, "_FLUSHED", True)
    assert hook._hard_stop_notice() is None  # output already final
    monkeypatch.setattr(hook, "_FLUSHED", False)
    monkeypatch.setattr(hook, "_RECALL_LANDED", True)
    assert hook._hard_stop_notice() is None  # recall already reached the model
    monkeypatch.setattr(hook, "_RECALL_LANDED", False)
    monkeypatch.setattr(hook, "_HOOK_MODE", "off")
    assert hook._hard_stop_notice() is None  # recall silenced on purpose
    monkeypatch.setattr(hook, "_HOOK_MODE", "server")

    # The room comes from the units actually written under the lock.
    written["units"] = HOOK_STDOUT_CAP - 3 - 20
    clipped = hook._hard_stop_notice()
    assert clipped is not None and len(clipped) <= 20  # stays under the harness cap
    written["units"] = HOOK_STDOUT_CAP
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
    assert writer.lines == [(hook._out_of_time_notice(age), "final")]


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
    assert all(block != "final" for _t, block in writer.lines)


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
    assert all(block != "final" for _t, block in writer.lines)


@pytest.mark.asyncio
async def test_off_mode_never_prints_the_notice(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Off mode silences recall on purpose; telling the model to recall would be wrong."""
    writer = _RecordingWriter()
    _quiet_run_body(monkeypatch, tmp_path, writer)
    monkeypatch.setattr(hook, "_HOOK_MODE", "off")
    await hook._run("anything", session_id="off", process_age_s=hook._RUN_DEADLINE_S + 1)
    assert all(block != "final" for _t, block in writer.lines)


@pytest.mark.asyncio
async def test_the_notice_survives_a_flush_that_used_up_the_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Codex #3096: a large deferred flush must not clip or drop the notice."""
    import io

    from hook_output import DEFAULT_BUDGET, BoundedStdout

    buf = io.StringIO()
    out = BoundedStdout(DEFAULT_BUDGET, label="proactive", reserve=hook._CUT_NOTICE_RESERVE, stream=buf)
    _quiet_run_body(monkeypatch, tmp_path, out)
    big = "t" * DEFAULT_BUDGET  # the trail alone exceeds the normal ceiling

    monkeypatch.setattr(hook, "_update_and_format_trail", lambda *_a, **_k: big)

    async def _slow_server(*_a, **_k):
        await asyncio.sleep(0.5)
        return None, "slow"

    monkeypatch.setattr(hook, "_call_server", _slow_server)
    await hook._run("why did recall stall", session_id="full", process_age_s=hook._RUN_DEADLINE_S - 0.05)
    hook._announce_cut()
    printed = buf.getvalue()
    # BOTH closing lines land whole: the reserve holds the cut notice AND this one.
    assert hook._out_of_time_notice(hook._RUN_DEADLINE_S - 0.05) in printed
    assert "output was CUT at 'session-metadata'" in printed


# ── PR-2: the per-step phase line (hook_phases.py) ──


class _Stderr:
    """One ordered record of both stderr channels: sys.stderr text and os.write(2)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.parts: list[str] = []
        real_write = hook.os.write

        def _write(fd: int, data: bytes) -> int:
            if fd == 2:
                self.parts.append(data.decode())
                return len(data)
            return real_write(fd, data)

        monkeypatch.setattr(hook.os, "write", _write)
        monkeypatch.setattr(sys, "stderr", self)

    def write(self, text: str) -> int:
        self.parts.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    @property
    def text(self) -> str:
        return "".join(self.parts)


def _phase_lines(err: str) -> list[dict]:
    import json

    prefix = "[hook-phases] "
    return [json.loads(ln[len(prefix) :]) for ln in err.splitlines() if ln.startswith(prefix)]


@pytest.mark.parametrize(
    "stdin_text",
    ["", "{not json", '{"session_id": "s"}'],
    ids=["empty", "bad-json", "no-prompt"],
)
def test_every_early_exit_writes_exactly_one_phase_line(
    monkeypatch: pytest.MonkeyPatch, stdin_text: str
) -> None:
    """Architect SF-1: the line comes from main()'s OUTERMOST finally, so the exits
    before the inner try (empty stdin, bad JSON) write it too."""
    import io

    err = _Stderr(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    hook.main()
    lines = _phase_lines(err.text)
    assert len(lines) == 1
    assert lines[0]["cut"] is False
    assert [name for name, _ in lines[0]["phases"]][-1] == "stdin"


def test_a_crash_writes_its_traceback_then_the_phase_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import json

    async def _boom(*_a, **_k) -> None:
        raise RuntimeError("run broke")

    monkeypatch.setattr(hook, "_run", _boom)
    capture = _Stderr(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "hi", "session_id": "s"})))
    hook.main()
    err = capture.text
    assert "RuntimeError: run broke" in err
    assert err.index("RuntimeError: run broke") < err.index("[hook-phases] ")
    assert len(_phase_lines(err)) == 1


@pytest.mark.asyncio
async def test_a_run_records_its_steps_path_and_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The degraded path's steps are marked in order and its run facts are set."""
    writer = _RecordingWriter()
    _quiet_run_body(monkeypatch, tmp_path, writer)

    async def _down(*_a, **_k):
        return None, "server unreachable"

    monkeypatch.setattr(hook, "_call_server", _down)
    monkeypatch.setattr(hook, "_search_fts5", lambda *_a, **_k: [])
    monkeypatch.setattr(hook, "_ambient_fold", lambda *_a, **_k: None)
    hook._PHASES.reset_run()
    await hook._run("what did we decide", session_id="s")
    import json

    line = json.loads(hook._phase_line()[len("[hook-phases] ") :])
    names = [name for name, _ in line["phases"]]
    run = names[names.index("heartbeat_write") :]
    assert run == [
        "heartbeat_write", "heartbeat_read", "trail", "activity", "recent_files",
        "gates", "migration", "suppress", "server", "fts5", "code_index", "degraded_emit",
        "flush", "shadow", "ws_measure", "activity_row", "metrics", "fold", "finish",
    ]  # fmt: skip
    assert line["path"] == "degraded"
    assert line["server_reason"] == "server unreachable"
    assert line["timed_out"] is False
    assert line["deadline_hit"] is False
    assert line["flushed"] is True


def test_the_running_hook_arms_its_stop_with_the_phase_line() -> None:
    """Run as a script, the hard stop carries a diag that renders a CUT phase line."""
    import os
    import subprocess

    probe = (
        "import runpy, sys, io, json;"
        "sys.argv=[sys.argv[1]];"
        "sys.stdin=io.StringIO('');"
        "g = runpy.run_path(sys.argv[0], run_name='__main__');"
        "diag = g['_HARD_STOP'].args[3];"
        "after_main = diag();"
        "diag.__globals__['_PHASE_LINE_WRITTEN'] = False;"
        "line = diag();"
        "p = json.loads(line[len('[hook-phases] '):]);"
        "print('RESULT', p['cut'], p['phases'][0][0], 'psi' in p, after_main)"
    )
    env = dict(os.environ)
    env.pop("GENESIS_CC_SESSION", None)
    out = subprocess.run(
        [sys.executable, "-c", probe, str(_SCRIPTS_DIR / "proactive_memory_hook.py")],
        capture_output=True, text=True, timeout=60, check=True, env=env,
    ).stdout  # fmt: skip
    # main() already wrote this run's line, so the stop's diag stands down (None);
    # before that, it renders a CUT line from the first module mark on.
    assert [ln for ln in out.splitlines() if ln.startswith("RESULT")] == [
        "RESULT True imports True None"
    ]


def test_a_stop_during_a_stuck_step_names_that_step(tmp_path: Path) -> None:
    """A REAL process: the heartbeat write blocks, the hard stop fires, and the cut
    line names heartbeat_write as running, after the stack sample."""
    import json
    import os
    import subprocess
    import textwrap

    child = textwrap.dedent(
        f"""
        import io, json, sys, time
        sys.path.insert(0, {str(_SCRIPTS_DIR)!r})
        sys.path.insert(0, {str(_SCRIPTS_DIR / "hooks")!r})
        import proactive_memory_hook as hook
        import hook_deadline as hd
        hook._heartbeat_write = lambda *a, **k: time.sleep(30)
        hook._DB_PATH = __import__("pathlib").Path({str(tmp_path / "genesis.db")!r})
        hd.arm(0.8, lambda: hook._hard_stop_notice(), label="t",
               diag=lambda: hook._phase_line(cut=True))
        sys.stdin = io.StringIO(json.dumps({{"prompt": "what did we decide", "session_id": "s"}}))
        hook.main()
        """
    )
    env = dict(os.environ)
    env.pop("GENESIS_CC_SESSION", None)
    env["HOME"] = str(tmp_path)
    proc = subprocess.run(
        [sys.executable, "-c", child], capture_output=True, text=True, timeout=60, env=env
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    lines = proc.stderr.splitlines()
    stack_at = next(i for i, ln in enumerate(lines) if "hard stop fired" in ln)
    phase_at = next(i for i, ln in enumerate(lines) if ln.startswith("[hook-phases] "))
    assert stack_at < phase_at
    cut = json.loads(lines[phase_at][len("[hook-phases] ") :])
    assert cut["cut"] is True
    assert cut["running"] == "heartbeat_write"
    assert cut["running_ms"] >= 500
    assert "[Memory: the hook ran out" in proc.stdout  # the notice still lands


@pytest.mark.asyncio
async def test_off_mode_books_its_flush_and_metrics_write_to_their_own_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review round 1 (Codex P2, Devin): the closing flush and off mode's metrics
    write were charged to `gates`, so a stall there named the wrong step."""
    import json

    writer = _RecordingWriter()
    _quiet_run_body(monkeypatch, tmp_path, writer)
    monkeypatch.setattr(hook, "_HOOK_MODE", "off")
    hook._PHASES.reset_run()
    await hook._run("what did we decide", session_id="s")
    line = json.loads(hook._phase_line()[len("[hook-phases] ") :])
    names = [name for name, _ in line["phases"]]
    assert names[names.index("gates") :] == ["gates", "flush", "metrics", "finish"]
    assert line["path"] == "off"


def test_deadline_hit_is_judged_when_the_line_is_written(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review round 1 (Devin): recorded before the closing flush, deadline_hit read
    False for a run whose flush crossed the deadline. It is now derived at render."""
    import json

    def _hit() -> object:
        return json.loads(hook._phase_line()[len("[hook-phases] ") :])["deadline_hit"]

    monkeypatch.setattr(hook, "_RUN_DEADLINE_AT", time.monotonic() - 0.01)
    assert _hit() is True
    monkeypatch.setattr(hook, "_RUN_DEADLINE_AT", time.monotonic() + 60)
    assert _hit() is False
    monkeypatch.setattr(hook, "_RUN_DEADLINE_AT", None)
    assert _hit() is None  # no run yet: unknown, not "on time"


def test_a_crash_while_the_module_loads_still_writes_a_phase_line() -> None:
    """Review round 1 (Codex P2, GLM P3): a load-time crash happens before main()
    exists. The hook's excepthook writes the line after Python's traceback."""
    import json
    import os
    import subprocess

    child = (
        "import runpy, sys, io;"
        "sys.modules['dotenv'] = None;"  # `from dotenv import ...` now raises
        "sys.argv = [sys.argv[1]];"
        "sys.stdin = io.StringIO('');"
        "runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    env = dict(os.environ)
    env.pop("GENESIS_CC_SESSION", None)
    proc = subprocess.run(
        [sys.executable, "-c", child, str(_SCRIPTS_DIR / "proactive_memory_hook.py")],
        capture_output=True, text=True, timeout=60, env=env,
    )  # fmt: skip
    assert proc.returncode == 1
    err = proc.stderr
    assert "Traceback" in err
    assert err.index("Traceback") < err.index("[hook-phases] ")
    lines = [ln for ln in err.splitlines() if ln.startswith("[hook-phases] ")]
    assert len(lines) == 1
    p = json.loads(lines[0][len("[hook-phases] ") :])
    assert p["startup_error"] in ("ImportError", "ModuleNotFoundError")
    assert p["phases"][-1][0] == "imports"  # the open step is where it crashed


def test_a_dispatched_session_exit_writes_no_line_even_with_the_excepthook() -> None:
    """The GENESIS_CC_SESSION early exit is a SystemExit, not a crash: no line."""
    import os
    import subprocess

    env = dict(os.environ)
    env["GENESIS_CC_SESSION"] = "1"
    proc = subprocess.run(
        [sys.executable, str(_SCRIPTS_DIR / "proactive_memory_hook.py")],
        input='{"prompt": "x", "session_id": "s"}', capture_output=True, text=True,
        timeout=60, env=env,
    )  # fmt: skip
    assert proc.returncode == 0
    assert "[hook-phases]" not in proc.stderr


@pytest.mark.asyncio
async def test_a_timed_out_run_books_its_closing_flush_before_finish(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fix-delta audit SF-1: on a timeout the body never flushed, so the finally's
    flush (and the notice) must land under `flush`, with `finish` after them."""
    import json

    writer = _RecordingWriter()
    _quiet_run_body(monkeypatch, tmp_path, writer)

    async def _slow_server(*_a, **_k):
        await asyncio.sleep(5)
        return None, "slow"

    monkeypatch.setattr(hook, "_call_server", _slow_server)
    monkeypatch.setattr(hook, "_RUN_DEADLINE_S", 0.2)
    hook._PHASES.reset_run()
    await hook._run("what did we decide", session_id="s")
    line = json.loads(hook._phase_line()[len("[hook-phases] ") :])
    names = [name for name, _ in line["phases"]]
    assert names[-3:] == ["server", "flush", "finish"]
    assert line["timed_out"] is True
    assert line["deadline_hit"] is True
