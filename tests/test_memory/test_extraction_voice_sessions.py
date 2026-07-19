"""E2E-in-test: voice transcripts flow through the real extraction cycle.

Producer and consumer are both REAL: the transcript comes from the actual
``VoiceTranscriptWriter`` and the cycle is the actual ``run_extraction_cycle``
against a full-schema DB — only the LLM router and the vector store are
mocked. This is the load-bearing W0.5 parity proof: a voice conversation is
discovered via the voice-dir fallback, mined, and watermarked exactly like
any other channel's session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.channels.voice.transcript_writer import (
    VoiceTranscriptWriter,
    transcript_session_id,
)
from genesis.db.schema import create_all_tables
from genesis.memory.extraction_job import run_extraction_cycle

_EXTRACTION_JSON = (
    '```json\n{"extractions": [{"content": "User prefers oat milk in coffee", '
    '"type": "preference", "confidence": 0.8, "entities": ["oat milk"]}], '
    '"session_topic": "coffee preferences", '
    '"session_keywords": ["coffee", "oat milk"]}\n```'
)


@pytest.mark.asyncio
async def test_voice_session_extracted_via_voice_dir_fallback(tmp_path, monkeypatch):
    cc_dir = tmp_path / "cc-projects"
    cc_dir.mkdir()
    voice_dir = tmp_path / "voice-transcripts"
    monkeypatch.setenv("GENESIS_VOICE_TRANSCRIPT_DIR", str(voice_dir))

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)

        # REAL producer: writer registers the session + writes the transcript
        writer = VoiceTranscriptWriter(db, transcript_dir=voice_dir)
        await writer.append_message("s2s-pe-042", "user", "remember I take oat milk")
        await writer.append_message("s2s-pe-042", "assistant", "noted — oat milk it is")
        await writer.close_session("s2s-pe-042")
        sid = transcript_session_id("s2s-pe-042")

        router = AsyncMock()
        router.route_call = AsyncMock(
            return_value=AsyncMock(
                success=True,
                content=_EXTRACTION_JSON,
                call_site_id="9_fact_extraction",
                error=None,
            )
        )
        store = AsyncMock()
        store.store = AsyncMock(return_value="mem-1")
        store._embeddings = MagicMock()
        store._embeddings.model_name = "m"

        summary = await run_extraction_cycle(
            db=db,
            store=store,
            router=router,
            transcript_dir=cc_dir,
        )

        assert summary["sessions_processed"] == 1
        assert summary["entities_extracted"] >= 1
        store.store.assert_awaited()

        cursor = await db.execute(
            "SELECT last_extracted_line, topic FROM cc_sessions WHERE id = ?",
            (sid,),
        )
        row = await cursor.fetchone()
        assert row["last_extracted_line"] > 0, "watermark must advance"
        assert row["topic"] == "coffee preferences"


@pytest.mark.asyncio
async def test_voice_watermark_makes_replay_a_noop(tmp_path, monkeypatch):
    """Second cycle over an unchanged transcript extracts nothing — the
    structural guarantee the legacy blob landing lacked."""
    cc_dir = tmp_path / "cc-projects"
    cc_dir.mkdir()
    voice_dir = tmp_path / "voice-transcripts"
    monkeypatch.setenv("GENESIS_VOICE_TRANSCRIPT_DIR", str(voice_dir))

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)

        writer = VoiceTranscriptWriter(db, transcript_dir=voice_dir)
        await writer.append_message("s2s-pe-042", "user", "hello there")
        await writer.append_message("s2s-pe-042", "assistant", "hi")

        router = AsyncMock()
        router.route_call = AsyncMock(
            return_value=AsyncMock(
                success=True,
                content=_EXTRACTION_JSON,
                call_site_id="9_fact_extraction",
                error=None,
            )
        )
        store = AsyncMock()
        store.store = AsyncMock(return_value="mem-1")
        store._embeddings = MagicMock()
        store._embeddings.model_name = "m"

        first = await run_extraction_cycle(
            db=db,
            store=store,
            router=router,
            transcript_dir=cc_dir,
        )
        assert first["sessions_processed"] == 1

        second = await run_extraction_cycle(
            db=db,
            store=store,
            router=router,
            transcript_dir=cc_dir,
        )
        assert second["chunks_processed"] == 0, (
            "replay must not re-extract already-watermarked lines"
        )
