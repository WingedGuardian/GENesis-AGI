"""Behavioral tests for the cross-session recency-resume block + its wiring.

Uses a real file-backed DB (build_recency_block opens its OWN read-only sqlite
connection by path, so :memory: — which is connection-private — won't do) and
the real transcript writer to produce correctly-formatted transcripts, then
pins ``last_activity_at`` to a controlled value so the time-based lookup is
deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.channels.voice import voice_recency
from genesis.channels.voice import voice_recency_resume_config as cfg
from genesis.channels.voice.transcript_writer import (
    VoiceTranscriptWriter,
    transcript_session_id,
)
from genesis.db.schema import create_all_tables

pytestmark = pytest.mark.asyncio

_NOW = datetime(2026, 8, 11, 12, 0, 0, tzinfo=UTC)


def _pin_config(monkeypatch, tmp_path, text: str) -> None:
    p = tmp_path / "voice_recency_resume.yaml"
    p.write_text(text)
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)


@pytest.fixture
def tdir(tmp_path, monkeypatch):
    d = tmp_path / "voice-transcripts"
    d.mkdir()
    monkeypatch.setattr(voice_recency, "voice_transcript_dir", lambda: d)
    return d


async def _db(tmp_path) -> tuple[aiosqlite.Connection, str]:
    dbp = tmp_path / "genesis.db"
    conn = await aiosqlite.connect(str(dbp))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    return conn, str(dbp)


async def _write_convo(conn, tdir, external_id, turns, *, last_activity, satellite_id=None):
    """Create a voice session + transcript via the real writer, then pin last_activity."""
    writer = VoiceTranscriptWriter(conn, transcript_dir=tdir)
    for role, text in turns:
        await writer.append_message(external_id, role, text, satellite_id=satellite_id)
    sid = transcript_session_id(external_id)
    await conn.execute("UPDATE cc_sessions SET last_activity_at=? WHERE id=?", (last_activity, sid))
    await conn.commit()
    return sid


async def test_off_by_default_returns_empty(tmp_path, tdir, monkeypatch):
    _pin_config(monkeypatch, tmp_path, "")  # no keys → defaults (mode off)
    conn, dbp = await _db(tmp_path)
    try:
        await _write_convo(
            conn,
            tdir,
            "s2s-k-20260811-000000",
            [("user", "hi"), ("assistant", "hello")],
            last_activity="2026-08-11T11:00:00+00:00",
        )
        assert voice_recency.build_recency_block(db_path=dbp, now=_NOW) == ""
    finally:
        await conn.close()


async def test_no_prior_session_returns_empty(tmp_path, tdir, monkeypatch):
    _pin_config(monkeypatch, tmp_path, "mode: live\n")
    conn, dbp = await _db(tmp_path)
    try:
        assert voice_recency.build_recency_block(db_path=dbp, now=_NOW) == ""
    finally:
        await conn.close()


async def test_resumes_prior_conversation(tmp_path, tdir, monkeypatch):
    _pin_config(monkeypatch, tmp_path, "mode: live\n")
    conn, dbp = await _db(tmp_path)
    try:
        await _write_convo(
            conn,
            tdir,
            "s2s-kitchen-20260811-000000",
            [("user", "let's talk about mars"), ("assistant", "sure, mars is fascinating")],
            last_activity="2026-08-11T11:00:00+00:00",
        )
        block = voice_recency.build_recency_block(db_path=dbp, now=_NOW)
        assert block.startswith("Where we left off (")  # age-stamped header
        assert "mars" in block
        assert "You:" in block and "Genesis:" in block
    finally:
        await conn.close()


async def test_excludes_current_session(tmp_path, tdir, monkeypatch):
    _pin_config(monkeypatch, tmp_path, "mode: live\n")
    conn, dbp = await _db(tmp_path)
    try:
        sid = await _write_convo(
            conn,
            tdir,
            "s2s-k-20260811-000000",
            [("user", "hi"), ("assistant", "yo")],
            last_activity="2026-08-11T11:00:00+00:00",
        )
        assert (
            voice_recency.build_recency_block(db_path=dbp, now=_NOW, current_session_id=sid) == ""
        )
    finally:
        await conn.close()


async def test_excludes_session_within_gap(tmp_path, tdir, monkeypatch):
    """A session active within the 60s gap is the live one — not resumed."""
    _pin_config(monkeypatch, tmp_path, "mode: live\n")
    conn, dbp = await _db(tmp_path)
    try:
        await _write_convo(
            conn,
            tdir,
            "s2s-k-20260811-000000",
            [("user", "hi"), ("assistant", "yo")],
            last_activity=(_NOW - timedelta(seconds=10)).isoformat(),
        )
        assert voice_recency.build_recency_block(db_path=dbp, now=_NOW) == ""
    finally:
        await conn.close()


async def test_quarantine_or_missing_transcript_fallthrough(tmp_path, tdir, monkeypatch):
    _pin_config(monkeypatch, tmp_path, "mode: live\n")
    conn, dbp = await _db(tmp_path)
    try:
        newest = await _write_convo(
            conn,
            tdir,
            "s2s-k-20260811-020000",
            [("user", "newest gone"), ("assistant", "q")],
            last_activity="2026-08-11T11:30:00+00:00",
        )
        (tdir / f"{newest}.jsonl").unlink()  # simulate quarantine / prune
        await _write_convo(
            conn,
            tdir,
            "s2s-k-20260811-010000",
            [("user", "older survives"), ("assistant", "ok")],
            last_activity="2026-08-11T11:00:00+00:00",
        )
        block = voice_recency.build_recency_block(db_path=dbp, now=_NOW)
        assert "older survives" in block
    finally:
        await conn.close()


async def test_per_device_scope(tmp_path, tdir, monkeypatch):
    _pin_config(monkeypatch, tmp_path, "mode: live\nscope: per_device\n")
    conn, dbp = await _db(tmp_path)
    try:
        await _write_convo(
            conn,
            tdir,
            "s2s-kitchen-20260811-010000",
            [("user", "kitchen chat"), ("assistant", "k")],
            last_activity="2026-08-11T11:00:00+00:00",
            satellite_id="kitchen",
        )
        await _write_convo(  # newer, different device — global would pick this
            conn,
            tdir,
            "s2s-office-20260811-020000",
            [("user", "office chat"), ("assistant", "o")],
            last_activity="2026-08-11T11:30:00+00:00",
            satellite_id="office",
        )
        block = voice_recency.build_recency_block(db_path=dbp, now=_NOW, satellite_id="kitchen")
        assert "kitchen chat" in block
        assert "office chat" not in block
    finally:
        await conn.close()


async def test_max_age_hours_excludes_old(tmp_path, tdir, monkeypatch):
    _pin_config(monkeypatch, tmp_path, "mode: live\nmax_age_hours: 2\n")
    conn, dbp = await _db(tmp_path)
    try:
        await _write_convo(
            conn,
            tdir,
            "s2s-k-20260811-000000",
            [("user", "ancient"), ("assistant", "x")],
            last_activity=(_NOW - timedelta(hours=5)).isoformat(),
        )
        assert voice_recency.build_recency_block(db_path=dbp, now=_NOW) == ""
    finally:
        await conn.close()


async def test_fail_closed_on_bad_db(tmp_path, tdir, monkeypatch):
    _pin_config(monkeypatch, tmp_path, "mode: live\n")
    assert voice_recency.build_recency_block(db_path=str(tmp_path / "nope.db"), now=_NOW) == ""


# --- get_system_prompt wiring (the block flows into the prompt when non-empty) ---


async def test_get_system_prompt_injects_recency(monkeypatch):
    from genesis.channels.voice import genesis_bridge
    from genesis.channels.voice import voice_recency as vr

    sentinel = "Where we left off (2m ago): You: test thread"
    monkeypatch.setattr(vr, "build_recency_block", lambda **kw: sentinel)
    out = genesis_bridge.GenesisBridge().get_system_prompt()
    assert sentinel in out


async def test_get_system_prompt_omits_when_empty(monkeypatch):
    from genesis.channels.voice import genesis_bridge
    from genesis.channels.voice import voice_recency as vr

    monkeypatch.setattr(vr, "build_recency_block", lambda **kw: "")
    out = genesis_bridge.GenesisBridge().get_system_prompt()
    assert "Where we left off" not in out
