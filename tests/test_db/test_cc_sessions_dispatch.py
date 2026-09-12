"""Tests for cc_sessions.dispatch_info_for_proposals — the join that surfaces
completed ego-dispatch findings in the dashboard proposal detail."""

from __future__ import annotations

import json

import pytest

from genesis.db.crud import cc_sessions as cc_crud


async def _mk_session(
    db,
    *,
    sid,
    proposal_id,
    output,
    transcript,
    started_at,
    caller_context=None,
    origin_caller_context=None,
):
    md = {
        "caller_context": caller_context or f"ego_proposal:{proposal_id}",
        "output_text": output,
        "transcript_path": transcript,
    }
    if origin_caller_context is not None:
        md["origin_caller_context"] = origin_caller_context
    await cc_crud.create(
        db,
        id=sid,
        session_type="background_task",
        model="opus",
        started_at=started_at,
        last_activity_at=started_at,
        source_tag="ego_dispatch",
        metadata=json.dumps(md),
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
async def test_resumed_session_matched_via_origin_caller_context(db):
    """A rate-limit-resumed dispatch has caller_context='rate_limit_resume:<park>'
    and carries the original 'ego_proposal:<id>' on origin_caller_context. The
    debrief join must find it via the origin (else the dashboard card is blank for
    exactly the resumed case). Pre-fix the WHERE matched only caller_context, so
    the resume-prefix row was excluded → KeyError here."""
    await _mk_session(
        db,
        sid="resumed",
        proposal_id="pR",
        output="finding after resume",
        transcript="/x/r.jsonl",
        started_at="2026-08-03T00:00:00+00:00",
        caller_context="rate_limit_resume:park-1",
        origin_caller_context="ego_proposal:pR",
    )
    out = await cc_crud.dispatch_info_for_proposals(db, ["pR"])
    assert out["pR"]["session_id"] == "resumed"
    assert out["pR"]["output_excerpt"] == "finding after resume"


@pytest.mark.asyncio
async def test_resumed_supersedes_original_parked(db):
    """When both the original parked dispatch and its later resume exist for one
    proposal, newest-wins surfaces the resume (the session that produced output)."""
    await _mk_session(
        db,
        sid="parked",
        proposal_id="pS",
        output="",  # parked original produced nothing before the limit
        transcript=None,
        started_at="2026-08-01T00:00:00+00:00",
    )
    await _mk_session(
        db,
        sid="resumed",
        proposal_id="pS",
        output="real finding",
        transcript="/x/s.jsonl",
        started_at="2026-08-04T00:00:00+00:00",
        caller_context="rate_limit_resume:park-2",
        origin_caller_context="ego_proposal:pS",
    )
    out = await cc_crud.dispatch_info_for_proposals(db, ["pS"])
    assert out["pS"]["session_id"] == "resumed"
    assert out["pS"]["output_excerpt"] == "real finding"


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
