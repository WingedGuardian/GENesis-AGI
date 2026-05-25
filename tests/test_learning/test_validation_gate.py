"""Tests for the self-learning validation gate."""

from __future__ import annotations

import pytest

from genesis.learning.procedural.validation_gate import (
    _check_evidence,
    _check_watermark,
    _jaccard_words,
    validate_extraction,
)

# --- Unit tests for _jaccard_words ---


def test_jaccard_identical():
    assert _jaccard_words("hello world", "hello world") == 1.0


def test_jaccard_disjoint():
    assert _jaccard_words("hello world", "foo bar") == 0.0


def test_jaccard_partial():
    result = _jaccard_words("hello world foo", "hello world bar")
    # intersection: {hello, world} = 2, union: {hello, world, foo, bar} = 4
    assert result == pytest.approx(0.5)


def test_jaccard_empty():
    assert _jaccard_words("", "hello") == 0.0
    assert _jaccard_words("hello", "") == 0.0


# --- Unit tests for _check_evidence ---


def test_evidence_no_impossibility_claim():
    """Non-impossibility principle passes with mod=1.0."""
    flags: list[str] = []
    result = _check_evidence("use yt-dlp for downloads", "ran yt-dlp", flags)
    assert result == 1.0
    assert flags == []


def test_evidence_impossibility_with_evidence():
    """Impossibility claim with error evidence passes."""
    flags: list[str] = []
    result = _check_evidence(
        "this API is broken and doesn't work",
        "HTTP 403 returned by the endpoint",
        flags,
    )
    assert result == 1.0
    assert "impossibility_claim" in flags
    assert "evidence_present" in flags


def test_evidence_impossibility_without_evidence():
    """Impossibility claim without evidence returns 0.0 (block)."""
    flags: list[str] = []
    result = _check_evidence(
        "this approach is impossible and won't work",
        "I tried a few things but nothing worked",
        flags,
    )
    assert result == 0.0
    assert "impossibility_claim" in flags
    assert "no_evidence_for_impossibility" in flags


def test_evidence_deprecated_with_evidence():
    """'Deprecated' claims with evidence pass."""
    flags: list[str] = []
    result = _check_evidence(
        "the old API is deprecated and no longer available",
        "Error: EndpointNotFound - API returned 404",
        flags,
    )
    assert result == 1.0
    assert "evidence_present" in flags


def test_evidence_cloudflare_counts_as_evidence():
    """Cloudflare/WAF mentions in summary count as evidence."""
    flags: list[str] = []
    result = _check_evidence(
        "automated access doesn't work on this site",
        "blocked by Cloudflare challenge page",
        flags,
    )
    assert result == 1.0
    assert "evidence_present" in flags


# --- Unit tests for _check_watermark ---


def test_watermark_none():
    """No watermark → no penalty."""
    flags: list[str] = []
    result = _check_watermark(None, "approach_failure", flags)
    assert result == 1.0


def test_watermark_regression_from_success():
    """Prior success + current failure → severe penalty."""
    flags: list[str] = []
    watermark = {"best_outcome": "success", "total_sessions": 3, "successful_sessions": 2}
    result = _check_watermark(watermark, "approach_failure", flags)
    assert result == 0.3
    assert "regression_from_success" in flags


def test_watermark_regression_from_workaround():
    """Prior workaround success + current failure → moderate penalty."""
    flags: list[str] = []
    watermark = {"best_outcome": "workaround_success", "total_sessions": 2, "successful_sessions": 0}
    result = _check_watermark(watermark, "approach_failure", flags)
    assert result == 0.5
    assert "regression_from_workaround" in flags


def test_watermark_no_regression():
    """Same or better outcome → no penalty."""
    flags: list[str] = []
    watermark = {"best_outcome": "approach_failure", "total_sessions": 1, "successful_sessions": 0}
    result = _check_watermark(watermark, "success", flags)
    assert result == 1.0
    assert flags == []


def test_watermark_equal_outcome_no_penalty():
    """Same outcome class as best → no penalty."""
    flags: list[str] = []
    watermark = {"best_outcome": "workaround_success", "total_sessions": 2, "successful_sessions": 0}
    result = _check_watermark(watermark, "workaround_success", flags)
    assert result == 1.0


# --- Integration tests for validate_extraction ---


@pytest.mark.asyncio
async def test_gate_allows_success_no_watermark(db):
    """First successful extraction passes with first_mover tag."""
    result = await validate_extraction(
        db,
        task_type="new-task",
        principle="use API to fetch data",
        steps=["call API", "parse response"],
        tools_used=["WebFetch"],
        outcome="success",
        summary_text="Called the API and got results",
        session_tools_count=3,
    )
    assert result.allowed is True
    assert result.first_mover is True
    assert result.adjusted_confidence == pytest.approx(0.5)  # 0.5 * min(1.0, 1.0) = 0.5
    assert "first_mover" in result.flags


@pytest.mark.asyncio
async def test_gate_blocks_impossibility_without_evidence(db):
    """Impossibility claim without evidence is blocked."""
    result = await validate_extraction(
        db,
        task_type="browser-task",
        principle="automated browser access is impossible on this site",
        steps=["tried to access"],
        tools_used=["browser_navigate"],
        outcome="approach_failure",
        summary_text="The site blocked access somehow",
        session_tools_count=2,
    )
    assert result.allowed is False
    assert "no_evidence_for_impossibility" in result.flags


@pytest.mark.asyncio
async def test_gate_allows_impossibility_with_evidence(db):
    """Impossibility claim with concrete evidence passes."""
    result = await validate_extraction(
        db,
        task_type="browser-task",
        principle="automated access doesn't work due to Cloudflare",
        steps=["navigate", "detect challenge"],
        tools_used=["browser_navigate"],
        outcome="approach_failure",
        summary_text="Cloudflare challenge page detected, HTTP 403",
        session_tools_count=5,
    )
    assert result.allowed is True
    assert "evidence_present" in result.flags


@pytest.mark.asyncio
async def test_gate_penalizes_failure_after_prior_success(db):
    """Failure extraction after a prior success gets reduced confidence."""
    # Seed watermark with a success
    from genesis.db.crud.watermarks import upsert_watermark

    await upsert_watermark(
        db, task_type="deploy-task", best_outcome="success",
        total_sessions=2, successful_sessions=1,
    )

    result = await validate_extraction(
        db,
        task_type="deploy-task",
        principle="deployment requires manual approval step",
        steps=["try deploy", "get blocked"],
        tools_used=["Bash"],
        outcome="approach_failure",
        summary_text="Deployment failed, needs approval",
        session_tools_count=4,
    )
    assert result.allowed is True
    assert result.adjusted_confidence == pytest.approx(0.15)  # 0.5 * 0.3 = 0.15
    assert "regression_from_success" in result.flags


@pytest.mark.asyncio
async def test_gate_updates_watermark(db):
    """Gate creates watermark on first extraction."""
    from genesis.db.crud.watermarks import get_watermark

    result = await validate_extraction(
        db,
        task_type="fresh-task",
        principle="do the thing",
        steps=["step1"],
        tools_used=["tool1"],
        outcome="success",
        summary_text="did the thing",
        session_tools_count=1,
    )
    assert result.allowed is True

    wm = await get_watermark(db, "fresh-task")
    assert wm is not None
    assert wm["best_outcome"] == "success"
    assert wm["total_sessions"] == 1
    assert wm["successful_sessions"] == 1


@pytest.mark.asyncio
async def test_gate_watermark_ratchets_upward(db):
    """Watermark best_outcome only ratchets up, never down."""
    from genesis.db.crud.watermarks import get_watermark, upsert_watermark

    await upsert_watermark(
        db, task_type="ratchet-task", best_outcome="success",
        total_sessions=1, successful_sessions=1,
    )

    # Now extract with a failure — watermark should keep 'success' as best
    await validate_extraction(
        db,
        task_type="ratchet-task",
        principle="try alternate approach",
        steps=["step"],
        tools_used=["tool"],
        outcome="approach_failure",
        summary_text="tried but failed",
        session_tools_count=2,
    )

    wm = await get_watermark(db, "ratchet-task")
    assert wm["best_outcome"] == "success"  # NOT downgraded
    assert wm["total_sessions"] == 2


@pytest.mark.asyncio
async def test_gate_fail_open_on_error(db):
    """Gate returns allowed=True on internal error (fail-open)."""
    # Close DB to force an error
    await db.close()

    result = await validate_extraction(
        db,
        task_type="error-task",
        principle="something",
        steps=["step"],
        tools_used=["tool"],
        outcome="success",
        summary_text="text",
        session_tools_count=1,
    )
    assert result.allowed is True
    assert result.adjusted_confidence == 0.5
    assert "gate_error_fail_open" in result.flags


@pytest.mark.asyncio
async def test_gate_confidence_floor(db):
    """Adjusted confidence never goes below 0.1."""
    from genesis.db.crud.watermarks import upsert_watermark

    await upsert_watermark(
        db, task_type="floor-task", best_outcome="success",
        total_sessions=5, successful_sessions=3,
    )

    result = await validate_extraction(
        db,
        task_type="floor-task",
        principle="this approach requires different tooling",
        steps=["step"],
        tools_used=["tool"],
        outcome="approach_failure",
        summary_text="tried different approach",
        session_tools_count=1,
    )
    # watermark_mod=0.3, confidence = max(0.1, 0.5 * 0.3) = 0.15
    assert result.allowed is True
    assert result.adjusted_confidence >= 0.1
