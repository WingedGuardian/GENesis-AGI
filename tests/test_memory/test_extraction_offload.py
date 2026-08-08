"""Incremental + off-loop transcript reading in run_extraction_cycle.

Covers the follow-up 470cec53 fix:
  * the transcript filesystem reads run OFF the event-loop thread
    (asyncio.to_thread), so a slow read never starves memory-recall;
  * the byte-offset watermark is written in lock-step with the line watermark
    (the INVARIANT: last_extracted_byte == byte start of line last_extracted_line)
    on BOTH the has-messages path and the empty-but-changed path;
  * a caught-up session stat-gates on the next cycle (no re-read, watermark stable).

Real-DB tests avoid AsyncMock fragility with the pre-loop procedure-rebuild drain.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from genesis.db.crud import cc_sessions
from genesis.util.jsonl import TranscriptDelta


def _user(text: str) -> str:
    return json.dumps({"type": "user", "message": {"content": text}}, ensure_ascii=False)


def _asst(text: str) -> str:
    return json.dumps(
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        ensure_ascii=False,
    )


def _summary() -> str:
    return json.dumps({"type": "summary", "summary": "meta"})


def _write_transcript(tmp_path, name: str, lines: list[str]):
    p = tmp_path / f"{name}.jsonl"
    p.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))
    return p


async def _mk_session(db, sid: str) -> None:
    await cc_sessions.create(
        db,
        id=sid,
        session_type="foreground",
        model="sonnet",
        started_at="2026-08-07T00:00:00",
        last_activity_at="2026-08-07T00:00:00",
        source_tag="foreground",
        status="active",
    )


# --------------------------------------------------------------------------- #
# off-loop: both _find_transcript and read_transcript_delta run in a worker
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_transcript_reads_run_off_the_event_loop(db, tmp_path, monkeypatch):
    from genesis.memory import extraction_job

    await _mk_session(db, "sess-off")
    loop_thread = threading.get_ident()
    seen: dict[str, int] = {}

    def spy_find(_dir, _cc_id):
        seen["find"] = threading.get_ident()
        return tmp_path / "sess-off.jsonl"

    def spy_delta(_path, *, start_line=0, start_byte=None, max_lines=None):
        seen["delta"] = threading.get_ident()
        # nothing new — empty + unchanged so the cycle short-circuits
        return TranscriptDelta(
            messages=[],
            new_byte_offset=start_byte or 0,
            new_line_count=start_line,
            unchanged=True,
        )

    monkeypatch.setattr(extraction_job, "_find_transcript", spy_find)
    monkeypatch.setattr(extraction_job, "read_transcript_delta", spy_delta)

    await extraction_job.run_extraction_cycle(
        db=db,
        store=AsyncMock(),
        router=AsyncMock(),
        transcript_dir=tmp_path,
    )

    assert seen.get("find") is not None, "_find_transcript never ran"
    assert seen.get("delta") is not None, "read_transcript_delta never ran"
    assert seen["find"] != loop_thread, "_find_transcript ran on the event-loop thread"
    assert seen["delta"] != loop_thread, "read_transcript_delta ran on the event-loop thread"


# --------------------------------------------------------------------------- #
# empty-but-changed path — dual-write invariant + next-cycle stat-gate
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_empty_change_advances_both_watermarks_then_stat_gates(db, tmp_path):
    from genesis.memory import extraction_job

    await _mk_session(db, "sess-empty")
    # three NON-message lines — read, but yield zero ConversationMessages
    p = _write_transcript(tmp_path, "sess-empty", [_summary(), _summary(), _summary()])
    size = p.stat().st_size

    await extraction_job.run_extraction_cycle(
        db=db,
        store=AsyncMock(),
        router=AsyncMock(),
        transcript_dir=tmp_path,
    )

    row = await cc_sessions.get_by_id(db, "sess-empty")
    # INVARIANT: byte offset == start of line `new_line_count` (== EOF here)
    assert row["last_extracted_line"] == 3
    assert row["last_extracted_byte"] == size

    # second cycle: size == stored byte → stat-gate → watermark unchanged
    await extraction_job.run_extraction_cycle(
        db=db,
        store=AsyncMock(),
        router=AsyncMock(),
        transcript_dir=tmp_path,
    )
    row2 = await cc_sessions.get_by_id(db, "sess-empty")
    assert row2["last_extracted_line"] == 3
    assert row2["last_extracted_byte"] == size


# --------------------------------------------------------------------------- #
# has-messages path — byte watermark tracks the line watermark (max_line)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_messages_path_writes_byte_in_lockstep_with_line(db, tmp_path, monkeypatch):
    from genesis.memory import extraction_job

    await _mk_session(db, "sess-msg")
    # 3 message lines → chunk_end line 2 → max_line 3; byte == EOF (start of line 3)
    p = _write_transcript(tmp_path, "sess-msg", [_user("a"), _asst("b"), _user("c")])
    size = p.stat().st_size

    # bypass the LLM: process chunks (advancing max_line + last_chunk_end_byte)
    # but store nothing, so no store/router/downstream complexity.
    async def fake_extract(**_kwargs):
        return SimpleNamespace(
            extractions=[],
            session_keywords=[],
            session_topic="",
            parse_error=None,
        )

    monkeypatch.setattr(extraction_job, "_extract_chunk", fake_extract)

    await extraction_job.run_extraction_cycle(
        db=db,
        store=AsyncMock(),
        router=AsyncMock(),
        transcript_dir=tmp_path,
    )

    row = await cc_sessions.get_by_id(db, "sess-msg")
    assert row["last_extracted_line"] == 3
    assert row["last_extracted_byte"] == size  # start of line 3, in lock-step

    # resume: no growth → stat-gate, watermark stable (proves the offset resumes)
    await extraction_job.run_extraction_cycle(
        db=db,
        store=AsyncMock(),
        router=AsyncMock(),
        transcript_dir=tmp_path,
    )
    row2 = await cc_sessions.get_by_id(db, "sess-msg")
    assert row2["last_extracted_byte"] == size


# --------------------------------------------------------------------------- #
# truncation/rotation WITH new content — line watermark must RESET in lock-step
# (regression guard for the has-messages-path max() staleness bug)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_truncation_with_new_content_resets_line_watermark(
    db,
    tmp_path,
    monkeypatch,
):
    from genesis.memory import extraction_job

    await _mk_session(db, "sess-trunc")

    async def fake_extract(**_kwargs):
        return SimpleNamespace(
            extractions=[],
            session_keywords=[],
            session_topic="",
            parse_error=None,
        )

    monkeypatch.setattr(extraction_job, "_extract_chunk", fake_extract)

    # cycle 1: a longer transcript (4 message lines) → watermark line=4, byte=size1
    _write_transcript(
        tmp_path,
        "sess-trunc",
        [_user("a"), _asst("b"), _user("c"), _asst("d")],
    )
    await extraction_job.run_extraction_cycle(
        db=db,
        store=AsyncMock(),
        router=AsyncMock(),
        transcript_dir=tmp_path,
    )
    assert (await cc_sessions.get_by_id(db, "sess-trunc"))["last_extracted_line"] == 4

    # cycle 2: transcript ROTATED to a SHORTER file with NEW content (2 lines).
    # size2 < stored byte → read_transcript_delta resets to a from-0 scan and
    # re-emits absolute line numbers starting at 0.
    p2 = _write_transcript(tmp_path, "sess-trunc", [_user("x"), _asst("y")])
    size2 = p2.stat().st_size
    await extraction_job.run_extraction_cycle(
        db=db,
        store=AsyncMock(),
        router=AsyncMock(),
        transcript_dir=tmp_path,
    )

    row = await cc_sessions.get_by_id(db, "sess-trunc")
    # INVARIANT: the line watermark must RESET to the new (shrunk) file — not
    # stay stale at 4 — and the byte must be the start of that line (== EOF).
    assert row["last_extracted_line"] == 2, "stale line watermark survived truncation"
    assert row["last_extracted_byte"] == size2


# --------------------------------------------------------------------------- #
# per-session cap break — watermarks stay in lock-step at the last processed
# chunk (both are set at the top of every chunk iteration, before the break)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_cap_break_writes_watermark_at_last_processed_chunk(
    db,
    tmp_path,
    monkeypatch,
):
    from genesis.memory import extraction_job
    from genesis.memory.extraction import Extraction

    await _mk_session(db, "sess-cap")
    lines = [_user("m0"), _asst("m1"), _user("m2")]
    _write_transcript(tmp_path, "sess-cap", lines)
    # byte offset of the start of line 1 == byte length of line 0 (+ newline)
    line0_end = len((lines[0] + "\n").encode("utf-8"))

    async def one_extraction(**_kwargs):
        return SimpleNamespace(
            extractions=[
                Extraction(content="fact zero", extraction_type="entity", confidence=0.9),
            ],
            session_keywords=[],
            session_topic="",
            parse_error=None,
        )

    monkeypatch.setattr(extraction_job, "_extract_chunk", one_extraction)

    # chunk_size=1 → one chunk per message; cap=1 → break after chunk 0 stores.
    await extraction_job.run_extraction_cycle(
        db=db,
        store=AsyncMock(),
        router=AsyncMock(),
        transcript_dir=tmp_path,
        chunk_size=1,
        max_extractions_per_session=1,
    )

    row = await cc_sessions.get_by_id(db, "sess-cap")
    # watermark reflects the LAST processed chunk (chunk 0 → line 1), byte in step
    assert row["last_extracted_line"] == 1
    assert row["last_extracted_byte"] == line0_end


# --------------------------------------------------------------------------- #
# transcript I/O failure — BOTH watermarks preserved (never advanced/initialized)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_io_failure_does_not_advance_watermark(db, tmp_path, monkeypatch):
    """A failed transcript read leaves BOTH watermarks untouched.

    Regression guard for the empty-branch write on a freshly-migrated session
    (line watermark set, byte watermark NULL): a ``failed=True`` delta must NOT
    persist ``byte=0`` against the nonzero line watermark — doing so violates the
    invariant and makes the next cycle seek byte 0 while numbering from the old
    line, re-reading the ENTIRE transcript (the multi-second loop-block this PR
    exists to remove).
    """
    from genesis.memory import extraction_job

    await _mk_session(db, "sess-iofail")
    # Migrated state: line watermark advanced, byte watermark still NULL.
    await cc_sessions.update_extraction_watermark(
        db, "sess-iofail", last_extracted_line=5, last_extracted_at="2026-08-07T00:00:00"
    )
    pre = await cc_sessions.get_by_id(db, "sess-iofail")
    assert pre["last_extracted_line"] == 5
    assert pre["last_extracted_byte"] is None  # the state the bug corrupts

    def failed_delta(_path, *, start_line=0, start_byte=None, max_lines=None):
        # what read_transcript_delta returns on a stat()/read() OSError
        return TranscriptDelta(
            messages=[],
            new_byte_offset=start_byte or 0,
            new_line_count=start_line,
            failed=True,
        )

    monkeypatch.setattr(
        extraction_job, "_find_transcript", lambda *_a, **_k: tmp_path / "sess-iofail.jsonl"
    )
    monkeypatch.setattr(extraction_job, "read_transcript_delta", failed_delta)

    await extraction_job.run_extraction_cycle(
        db=db,
        store=AsyncMock(),
        router=AsyncMock(),
        transcript_dir=tmp_path,
    )

    row = await cc_sessions.get_by_id(db, "sess-iofail")
    assert row["last_extracted_line"] == 5  # line watermark unchanged
    assert row["last_extracted_byte"] is None  # byte watermark NOT advanced to 0
