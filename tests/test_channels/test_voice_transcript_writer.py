"""Tests for the voice transcript writer (W0.5 extraction parity).

The round-trip tests parse writer output with the REAL
``read_transcript_messages`` — the writer's whole contract is producing
transcripts the extraction job can mine, so the real parser is the oracle.
"""

from __future__ import annotations

import pytest

from genesis.channels.voice.transcript_writer import (
    VoiceTranscriptWriter,
    transcript_session_id,
    validate_conversation,
)
from genesis.db.crud import cc_sessions as sessions_crud
from genesis.util.jsonl import read_transcript_messages


def _turns(n: int) -> list[dict]:
    out = []
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        out.append({"role": role, "text": f"turn {i}"})
    return out


# ── validate_conversation (pure) ─────────────────────────────────────


class TestValidateConversation:
    def test_valid_body_passes(self):
        body = {"session_id": "edge-1", "satellite_id": "pe", "turns": _turns(4)}
        assert validate_conversation(body) == []

    def test_empty_turns_list_is_valid(self):
        assert validate_conversation({"session_id": "s", "turns": []}) == []

    @pytest.mark.parametrize(
        ("overrides", "fragment"),
        [
            ({"session_id": ""}, "session_id"),
            ({"session_id": 42}, "session_id"),
            ({"session_id": "x" * 200}, "128"),
            ({"satellite_id": 7}, "satellite_id"),
            ({"turns": "nope"}, "turns"),
            ({"turns": [{"role": "system", "text": "hi"}]}, "role"),
            ({"turns": [{"role": "user", "text": ""}]}, "text"),
            ({"turns": [{"role": "user"}]}, "text"),
            ({"turns": ["not-a-dict"]}, "object"),
        ],
    )
    def test_each_violation_yields_its_error(self, overrides, fragment):
        body = {"session_id": "edge-1", "turns": _turns(2)}
        body.update(overrides)
        errors = validate_conversation(body)
        assert errors, f"expected errors for {overrides}"
        assert any(fragment in e for e in errors)


# ── deterministic session identity ───────────────────────────────────


def test_transcript_session_id_deterministic_and_distinct():
    assert transcript_session_id("a") == transcript_session_id("a")
    assert transcript_session_id("a") != transcript_session_id("b")
    # Valid UUID shape (used as filename + cc_sessions primary key)
    import uuid

    uuid.UUID(transcript_session_id("a"))


# ── writer behavior (real DB + real parser) ──────────────────────────


@pytest.mark.asyncio
class TestVoiceTranscriptWriter:
    @pytest.fixture
    def writer(self, db, tmp_path):
        return VoiceTranscriptWriter(db, transcript_dir=tmp_path / "voice")

    async def test_append_round_trips_through_real_parser(self, writer, tmp_path):
        await writer.append_message("s2s-pe-1", "user", "what time is it")
        await writer.append_message("s2s-pe-1", "assistant", "half past three")
        await writer.append_message("s2s-pe-1", "user", "thanks")

        sid = transcript_session_id("s2s-pe-1")
        messages = read_transcript_messages(tmp_path / "voice" / f"{sid}.jsonl")
        assert [(m.role, m.text) for m in messages] == [
            ("user", "what time is it"),
            ("assistant", "half past three"),
            ("user", "thanks"),
        ]
        assert all(m.timestamp for m in messages)

    async def test_registers_voice_session_row_once(self, writer, db):
        await writer.append_message("s2s-pe-1", "user", "hello")
        await writer.append_message("s2s-pe-1", "assistant", "hi")

        sid = transcript_session_id("s2s-pe-1")
        row = await sessions_crud.get_by_id(db, sid)
        assert row is not None
        assert row["source_tag"] == "voice"
        assert row["channel"] == "voice_s2s"
        assert row["session_type"] == "foreground"
        assert row["status"] == "active"
        assert row["cc_session_id"] == sid

    async def test_close_session_completes_row(self, writer, db):
        await writer.append_message("s2s-pe-1", "user", "hello")
        await writer.close_session("s2s-pe-1")
        row = await sessions_crud.get_by_id(db, transcript_session_id("s2s-pe-1"))
        assert row["status"] == "completed"

    async def test_sync_cumulative_is_idempotent(self, writer):
        assert await writer.sync_cumulative("edge-1", _turns(3)) == 3
        # Exact replay (edge double-fire) appends nothing
        assert await writer.sync_cumulative("edge-1", _turns(3)) == 0
        # Superset appends only the delta
        assert await writer.sync_cumulative("edge-1", _turns(5)) == 2

    async def test_sync_cumulative_replay_leaves_file_identical(self, writer, tmp_path):
        await writer.sync_cumulative("edge-1", _turns(4))
        sid = transcript_session_id("edge-1")
        path = tmp_path / "voice" / f"{sid}.jsonl"
        first = path.read_text()
        await writer.sync_cumulative("edge-1", _turns(4))
        assert path.read_text() == first

    async def test_shorter_list_appends_nothing_and_warns(self, writer, tmp_path, caplog):
        await writer.sync_cumulative("edge-1", _turns(5))
        with caplog.at_level("WARNING"):
            assert await writer.sync_cumulative("edge-1", _turns(2)) == 0
        assert any("cache reset" in r.message for r in caplog.records)
        sid = transcript_session_id("edge-1")
        messages = read_transcript_messages(tmp_path / "voice" / f"{sid}.jsonl")
        assert len(messages) == 5

    async def test_new_writer_instance_continues_same_transcript(self, db, tmp_path):
        """Restart survival: the deterministic id + on-disk line count carry
        the reconciliation state — no in-memory state required."""
        w1 = VoiceTranscriptWriter(db, transcript_dir=tmp_path / "voice")
        await w1.sync_cumulative("edge-1", _turns(3))
        w2 = VoiceTranscriptWriter(db, transcript_dir=tmp_path / "voice")
        assert await w2.sync_cumulative("edge-1", _turns(3)) == 0
        assert await w2.sync_cumulative("edge-1", _turns(4)) == 1

    async def test_heal_orphans_completes_only_idle_voice_actives(self, db, tmp_path):
        from datetime import UTC, datetime

        await sessions_crud.register_voice_session(
            db,
            id="voice-orphan",
            started_at="2026-07-01T00:00:00+00:00",
        )
        # A voice session active RIGHT NOW (fresh last_activity_at) must
        # never be healed mid-call — the idle gate protects it.
        await sessions_crud.register_voice_session(
            db,
            id="voice-live",
            started_at=datetime.now(UTC).isoformat(),
        )
        await sessions_crud.create(
            db,
            id="cc-foreground",
            session_type="foreground",
            model="opus",
            started_at="2026-07-01T00:00:00+00:00",
            last_activity_at="2026-07-01T00:00:00+00:00",
            status="active",
        )
        writer = VoiceTranscriptWriter(db, transcript_dir=tmp_path / "voice")
        assert await writer.heal_orphans() == 1
        assert (await sessions_crud.get_by_id(db, "voice-orphan"))["status"] == "completed"
        assert (await sessions_crud.get_by_id(db, "voice-live"))["status"] == "active"
        assert (await sessions_crud.get_by_id(db, "cc-foreground"))["status"] == "active"

    async def test_empty_cumulative_sync_registers_nothing(self, writer, db, tmp_path):
        assert await writer.sync_cumulative("edge-empty", []) == 0
        sid = transcript_session_id("edge-empty")
        assert await sessions_crud.get_by_id(db, sid) is None
        assert not (tmp_path / "voice" / f"{sid}.jsonl").exists()

    async def test_append_failure_raises_not_swallows(self, db, tmp_path):
        """Durable-before-ack: an unwritable transcript dir must RAISE (the
        route answers 5xx and the producer retries) — a swallowed error
        would ack turns that were never written."""
        blocked = tmp_path / "blocked"
        blocked.write_text("a file, not a dir")
        writer = VoiceTranscriptWriter(db, transcript_dir=blocked)
        with pytest.raises(OSError):
            await writer.sync_cumulative("edge-1", _turns(2))

    async def test_append_ignores_invalid_role_and_blank_text(self, writer, tmp_path):
        await writer.append_message("edge-1", "system", "nope")
        await writer.append_message("edge-1", "user", "   ")
        sid = transcript_session_id("edge-1")
        assert not (tmp_path / "voice" / f"{sid}.jsonl").exists()
