"""Phantom-signal claim guard — annotate-and-strip, never blocking."""

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.reflection.signal_claims import (
    guard_narrative,
    validate_signal_claims,
)

_REGISTRY = {"memory_backlog", "autonomy_activity", "user_session_pattern"}


def test_strips_registered_claim_absent_from_tick():
    text = "Status stable, but memory_backlog=0.8 needs attention."
    cleaned, violations = validate_signal_claims(
        text, tick_signal_names={"autonomy_activity"}, registry_names=_REGISTRY,
    )
    assert violations == ["memory_backlog"]
    assert "[unverified signal claim removed: memory_backlog]" in cleaned
    assert "memory_backlog=0.8" not in cleaned


def test_keeps_claim_present_in_tick():
    text = "autonomy_activity: 1.0 remains pinned."
    cleaned, violations = validate_signal_claims(
        text, tick_signal_names={"autonomy_activity"}, registry_names=_REGISTRY,
    )
    assert violations == []
    assert cleaned == text


def test_unregistered_names_are_prose_not_claims():
    text = "error_rate=0.02 and coffee_level: 9000 are not registered signals."
    cleaned, violations = validate_signal_claims(
        text, tick_signal_names=set(), registry_names=_REGISTRY,
    )
    assert violations == []
    assert cleaned == text


def test_none_tick_names_skips_validation():
    text = "memory_backlog=0.8"
    cleaned, violations = validate_signal_claims(
        text, tick_signal_names=None, registry_names=_REGISTRY,
    )
    assert violations == []
    assert cleaned == text


def test_empty_registry_skips_validation():
    cleaned, violations = validate_signal_claims(
        "memory_backlog=0.8", tick_signal_names=set(), registry_names=set(),
    )
    assert violations == []


def test_prose_without_numbers_untouched():
    text = "The memory_backlog signal has been discussed before."
    cleaned, violations = validate_signal_claims(
        text, tick_signal_names=set(), registry_names=_REGISTRY,
    )
    assert violations == []
    assert cleaned == text


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        yield conn


async def _register(db, *names):
    for n in names:
        await db.execute(
            "INSERT OR IGNORE INTO signal_weights (signal_name, source_mcp, "
            "current_weight, initial_weight, feeds_depths) "
            "VALUES (?, 'test', 0.5, 0.5, ?)",
            (n, '["Deep"]'),
        )
    await db.commit()


@pytest.mark.asyncio
async def test_guard_narrative_strips_and_records_observation(db):
    await _register(db, "memory_backlog")
    cleaned = await guard_narrative(
        db, "Claim: memory_backlog=0.9 is critical.",
        tick_signal_names={"other_signal"}, source="deep_reflection",
    )
    assert "[unverified signal claim removed: memory_backlog]" in cleaned
    rows = await db.execute_fetchall(
        "SELECT content FROM observations WHERE type='phantom_signal_claim'"
    )
    assert len(rows) == 1
    assert "memory_backlog" in rows[0]["content"]


@pytest.mark.asyncio
async def test_guard_narrative_clean_text_no_observation(db):
    await _register(db, "memory_backlog")
    text = "All quiet on every front."
    cleaned = await guard_narrative(
        db, text, tick_signal_names={"memory_backlog"}, source="deep_reflection",
    )
    assert cleaned == text
    rows = await db.execute_fetchall(
        "SELECT id FROM observations WHERE type='phantom_signal_claim'"
    )
    assert rows == []


@pytest.mark.asyncio
async def test_guard_narrative_internal_error_passthrough():
    class BoomDB:
        async def execute(self, *a, **k):
            raise RuntimeError("db down")

    text = "memory_backlog=0.9"
    cleaned = await guard_narrative(
        BoomDB(), text, tick_signal_names=set(), source="deep_reflection",
    )
    assert cleaned == text  # guard failure NEVER loses the narrative


@pytest.mark.asyncio
async def test_guard_narrative_none_tick_passthrough(db):
    text = "memory_backlog=0.9"
    assert await guard_narrative(
        db, text, tick_signal_names=None, source="x",
    ) == text
