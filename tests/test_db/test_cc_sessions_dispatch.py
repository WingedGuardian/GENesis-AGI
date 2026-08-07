"""Tests for cc_sessions.dispatch_info_for_proposals — the join that surfaces
completed ego-dispatch findings in the dashboard proposal detail."""

from __future__ import annotations

import json

import pytest

from genesis.db.crud import cc_sessions as cc_crud


async def _mk_session(db, *, sid, proposal_id, output, transcript, started_at):
    await cc_crud.create(
        db,
        id=sid,
        session_type="background_task",
        model="opus",
        started_at=started_at,
        last_activity_at=started_at,
        source_tag="ego_dispatch",
        metadata=json.dumps(
            {
                "caller_context": f"ego_proposal:{proposal_id}",
                "output_text": output,
                "transcript_path": transcript,
            }
        ),
    )


@pytest.mark.asyncio
async def test_maps_proposal_to_dispatch_output(db):
    await _mk_session(
        db,
        sid="s1",
        proposal_id="pA",
        output="finding A body",
        transcript="/x/a.jsonl",
        started_at="2026-08-01T00:00:00+00:00",
    )
    out = await cc_crud.dispatch_info_for_proposals(db, ["pA", "pMissing"])
    assert set(out) == {"pA"}  # unmatched proposal id absent
    assert out["pA"]["session_id"] == "s1"
    assert out["pA"]["output_excerpt"] == "finding A body"
    assert out["pA"]["transcript_path"] == "/x/a.jsonl"


@pytest.mark.asyncio
async def test_newest_session_wins(db):
    await _mk_session(
        db,
        sid="old",
        proposal_id="pB",
        output="OLD",
        transcript="/x/old.jsonl",
        started_at="2026-08-01T00:00:00+00:00",
    )
    await _mk_session(
        db,
        sid="new",
        proposal_id="pB",
        output="NEW",
        transcript="/x/new.jsonl",
        started_at="2026-08-05T00:00:00+00:00",
    )
    out = await cc_crud.dispatch_info_for_proposals(db, ["pB"])
    assert out["pB"]["session_id"] == "new"
    assert out["pB"]["output_excerpt"] == "NEW"


@pytest.mark.asyncio
async def test_empty_ids_returns_empty(db):
    assert await cc_crud.dispatch_info_for_proposals(db, []) == {}


@pytest.mark.asyncio
async def test_malformed_metadata_row_is_skipped_not_fatal(db):
    # A legacy/corrupt row with non-JSON metadata must be filtered by json_valid,
    # not raise a "malformed JSON" error that breaks the whole join.
    await cc_crud.create(
        db,
        id="bad",
        session_type="background_task",
        model="opus",
        started_at="2026-08-02T00:00:00+00:00",
        last_activity_at="2026-08-02T00:00:00+00:00",
        metadata="this is not json",
    )
    await _mk_session(
        db,
        sid="good",
        proposal_id="pD",
        output="D",
        transcript="/x/d.jsonl",
        started_at="2026-08-01T00:00:00+00:00",
    )
    out = await cc_crud.dispatch_info_for_proposals(db, ["pD"])
    assert out["pD"]["session_id"] == "good"  # good row returned, bad row skipped


@pytest.mark.asyncio
async def test_excerpt_capped(db):
    await _mk_session(
        db,
        sid="big",
        proposal_id="pC",
        output="x" * 9000,
        transcript=None,
        started_at="2026-08-01T00:00:00+00:00",
    )
    out = await cc_crud.dispatch_info_for_proposals(db, ["pC"])
    assert len(out["pC"]["output_excerpt"]) == 4000
