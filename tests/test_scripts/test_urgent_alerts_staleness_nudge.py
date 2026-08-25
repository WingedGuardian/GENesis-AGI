"""Tests for the MCP stale-code nudge in genesis_urgent_alerts.

The nudge fires on EVERY prompt but must be SILENT unless this session's MCP
subprocesses run code OLDER than the last deploy — so the omission matrix
(fresh / ahead / no deploy / no spawn record / throttled / no session pid) is
the contract that keeps it free, and it must reuse the SAME commit_identity
verdict the dashboard badge uses (a session ahead of the deploy is never
flagged). Fail-open throughout.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"

_ua_spec = importlib.util.spec_from_file_location(
    "genesis_urgent_alerts", _SCRIPTS_DIR / "genesis_urgent_alerts.py"
)
_ua = importlib.util.module_from_spec(_ua_spec)
_ua_spec.loader.exec_module(_ua)

SID = "sid-stale-1"
PID = 424242
PROC_START = "2026-08-15T00:00:00+00:00"
SPAWN_AT = "2026-08-15T00:00:05+00:00"  # a beat after the process start
SPAWN_COMMIT = "176f5b3b8bfd1e11907fafc92967fe1e956330b1"  # full SHA (rev-parse)
DEPLOY_COMMIT = "87590955"  # short SHA (update_history.new_commit)
DEPLOY_AT = "2026-08-21T19:48:50+00:00"  # AFTER spawn → behind → stale
NOW = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)


# ── _staleness_message (pure verdict + wording, no IO) ──────────────────────


def test_message_stale_session_behind_deploy():
    msg = _ua._staleness_message(SPAWN_COMMIT, SPAWN_AT, (DEPLOY_AT, DEPLOY_COMMIT))
    assert msg is not None
    assert "stale" in msg.lower()
    assert "2026-08-21" in msg  # deploy date
    assert DEPLOY_COMMIT[:8] in msg
    assert msg.startswith("[") and msg.endswith("]")


def test_message_fresh_same_commit_silent():
    # spawn commit == deploy commit (prefix match via same_commit) → not stale
    assert _ua._staleness_message(DEPLOY_COMMIT, SPAWN_AT, (DEPLOY_AT, DEPLOY_COMMIT)) is None


def test_message_session_ahead_of_deploy_silent():
    # spawn_at AFTER the deploy completed → session is ahead (manual git pull),
    # NOT stale, even though commits differ.
    ahead_spawn = "2026-08-22T00:00:00+00:00"
    assert _ua._staleness_message(SPAWN_COMMIT, ahead_spawn, (DEPLOY_AT, DEPLOY_COMMIT)) is None


def test_message_no_deploy_silent():
    assert _ua._staleness_message(SPAWN_COMMIT, SPAWN_AT, None) is None


def test_message_nonhex_commit_silent():
    # defense-in-depth: a malformed new_commit must never reach LLM-visible context
    assert _ua._staleness_message(SPAWN_COMMIT, SPAWN_AT, (DEPLOY_AT, "not-a-sha!!")) is None
    assert _ua._staleness_message(SPAWN_COMMIT, SPAWN_AT, (DEPLOY_AT, "")) is None


# ── _last_successful_deploy (read-only sqlite) ──────────────────────────────


def _make_update_db(tmp_path: Path, rows: list[tuple[str, str, str]]) -> Path:
    """rows = [(status, new_commit, completed_at), ...]."""
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(root / "data" / "genesis.db")
    conn.execute(
        "CREATE TABLE update_history (id TEXT, status TEXT, new_commit TEXT, completed_at TEXT)"
    )
    for i, (status, commit, completed) in enumerate(rows):
        conn.execute(
            "INSERT INTO update_history (id, status, new_commit, completed_at) VALUES (?,?,?,?)",
            (f"u{i}", status, commit, completed),
        )
    conn.commit()
    conn.close()
    return root


def test_last_deploy_newest_success(tmp_path):
    root = _make_update_db(
        tmp_path,
        [
            ("success", "aaaa1111", "2026-08-20T00:00:00+00:00"),
            ("success", DEPLOY_COMMIT, DEPLOY_AT),  # newest success
            ("failed", "bbbb2222", "2026-08-22T00:00:00+00:00"),  # newer but not success
        ],
    )
    got = _ua._last_successful_deploy(root / "data" / "genesis.db")
    assert got == (DEPLOY_AT, DEPLOY_COMMIT)


def test_last_deploy_no_success_rows(tmp_path):
    root = _make_update_db(tmp_path, [("failed", "bbbb2222", "2026-08-22T00:00:00+00:00")])
    assert _ua._last_successful_deploy(root / "data" / "genesis.db") is None


def test_last_deploy_missing_db(tmp_path):
    assert _ua._last_successful_deploy(tmp_path / "nowhere" / "genesis.db") is None


def test_last_deploy_missing_table(tmp_path):
    root = tmp_path / "repo"
    (root / "data").mkdir(parents=True)
    sqlite3.connect(root / "data" / "genesis.db").close()
    assert _ua._last_successful_deploy(root / "data" / "genesis.db") is None


# ── throttle marker ─────────────────────────────────────────────────────────


def test_throttle_absent_then_recorded(monkeypatch, tmp_path):
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    assert _ua._staleness_throttled(SID, NOW) is False  # no marker yet
    assert _ua._record_staleness_nudge(SID, NOW) is True  # persisted
    assert _ua._staleness_throttled(SID, NOW) is True  # within cooldown
    # well past the cooldown → not throttled
    later = NOW + timedelta(seconds=_ua._STALENESS_COOLDOWN_S + 60)
    assert _ua._staleness_throttled(SID, later) is False


def test_record_returns_false_when_unwritable(monkeypatch, tmp_path):
    # A file where the "sessions" dir should be → mkdir of sessions/<id> fails.
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    (tmp_path / "sessions").write_text("not a dir")
    assert _ua._record_staleness_nudge(SID, NOW) is False


def test_throttle_garbled_marker_not_throttled(monkeypatch, tmp_path):
    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    marker = tmp_path / "sessions" / SID / "staleness_last_nudge"
    marker.parent.mkdir(parents=True)
    marker.write_text("not-a-timestamp")
    assert _ua._staleness_throttled(SID, NOW) is False


# ── _emit_staleness_nudge (integration; spawn plane + /proc monkeypatched) ──


def _wire(monkeypatch, tmp_path, *, pid, slots, ident, deploy_rows):
    """Point every IO seam at fakes/tmp. `slots` = enumerate_spawn_slots result;
    `ident` = read_spawn_identity result."""
    import genesis.observability.cc_slots as cc_slots
    import genesis.observability.mcp_spawn_store as sp

    monkeypatch.setattr(_ua, "_GENESIS_DIR", tmp_path)
    monkeypatch.setattr(_ua, "_claude_ancestor_pid", lambda: pid)
    monkeypatch.setattr(sp, "enumerate_spawn_slots", lambda: slots)
    monkeypatch.setattr(sp, "read_spawn_identity", lambda *a, **k: ident)
    monkeypatch.setattr(cc_slots, "read_proc_start_iso", lambda _p: PROC_START)
    root = _make_update_db(tmp_path, deploy_rows)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(root))


_STALE_ROWS = [("success", DEPLOY_COMMIT, DEPLOY_AT)]


def test_emit_stale_prints_and_records(monkeypatch, capsys, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(SPAWN_COMMIT, SPAWN_AT),
        deploy_rows=_STALE_ROWS,
    )
    _ua._emit_staleness_nudge(SID, NOW)
    out = capsys.readouterr().out
    assert "stale" in out.lower() and DEPLOY_COMMIT[:8] in out
    # marker recorded → second call within cooldown is silent
    _ua._emit_staleness_nudge(SID, NOW)
    assert capsys.readouterr().out == ""


def test_emit_fresh_silent(monkeypatch, capsys, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(DEPLOY_COMMIT, SPAWN_AT),  # same commit as deploy → not stale
        deploy_rows=_STALE_ROWS,
    )
    _ua._emit_staleness_nudge(SID, NOW)
    assert capsys.readouterr().out == ""


def test_emit_no_claude_pid_silent(monkeypatch, capsys, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        pid=None,
        slots=[("1", PID, SPAWN_AT)],
        ident=(SPAWN_COMMIT, SPAWN_AT),
        deploy_rows=_STALE_ROWS,
    )
    _ua._emit_staleness_nudge(SID, NOW)
    assert capsys.readouterr().out == ""


def test_emit_pid_not_in_spawn_plane_silent(monkeypatch, capsys, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", 999999, SPAWN_AT)],  # a DIFFERENT session's pid
        ident=(SPAWN_COMMIT, SPAWN_AT),
        deploy_rows=_STALE_ROWS,
    )
    _ua._emit_staleness_nudge(SID, NOW)
    assert capsys.readouterr().out == ""


def test_emit_unvalidated_ident_silent(monkeypatch, capsys, tmp_path):
    # read_spawn_identity rejects (pid mismatch / recycled) → None → silent
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=None,
        deploy_rows=_STALE_ROWS,
    )
    _ua._emit_staleness_nudge(SID, NOW)
    assert capsys.readouterr().out == ""


def test_emit_throttled_silent(monkeypatch, capsys, tmp_path):
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(SPAWN_COMMIT, SPAWN_AT),
        deploy_rows=_STALE_ROWS,
    )
    _ua._record_staleness_nudge(SID, NOW)  # pre-stamp the cooldown
    _ua._emit_staleness_nudge(SID, NOW)
    assert capsys.readouterr().out == ""


def test_emit_suppressed_when_marker_unwritable(monkeypatch, capsys, tmp_path):
    # If the cooldown marker can't persist, suppress rather than spam every prompt.
    _wire(
        monkeypatch,
        tmp_path,
        pid=PID,
        slots=[("1", PID, SPAWN_AT)],
        ident=(SPAWN_COMMIT, SPAWN_AT),
        deploy_rows=_STALE_ROWS,
    )
    monkeypatch.setattr(_ua, "_record_staleness_nudge", lambda *a, **k: False)
    _ua._emit_staleness_nudge(SID, NOW)
    assert capsys.readouterr().out == ""
