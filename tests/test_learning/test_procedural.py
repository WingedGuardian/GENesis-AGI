"""Tests for procedural memory — maturity, operations, matcher."""

from __future__ import annotations

import json

import pytest

from genesis.learning.procedural.matcher import find_best_match
from genesis.learning.procedural.maturity import get_maturity_stage
from genesis.learning.procedural.operations import (
    record_failure,
    record_success,
    record_workaround,
    store_procedure,
    update_confidence,
)
from genesis.learning.types import MaturityStage

# ─── Maturity ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_maturity_early_empty(db):
    assert await get_maturity_stage(db) == MaturityStage.EARLY


@pytest.mark.asyncio
async def test_maturity_growing(db):
    for _i in range(55):
        await store_procedure(
            db, task_type="t", principle="p", steps=["s"],
            tools_used=["tool"], context_tags=["tag"],
        )
    assert await get_maturity_stage(db) == MaturityStage.GROWING


@pytest.mark.asyncio
async def test_maturity_ignores_deprecated(db):
    for _i in range(55):
        await store_procedure(
            db, task_type="t", principle="p", steps=["s"],
            tools_used=["tool"], context_tags=["tag"],
        )
    # Deprecate 10 so we drop below 50
    cursor = await db.execute(
        "SELECT id FROM procedural_memory LIMIT 10"
    )
    rows = await cursor.fetchall()
    for r in rows:
        await db.execute(
            "UPDATE procedural_memory SET deprecated = 1 WHERE id = ?", (r[0],)
        )
    await db.commit()
    assert await get_maturity_stage(db) == MaturityStage.EARLY


@pytest.mark.asyncio
async def test_maturity_mature(db):
    # Insert 201 rows directly for speed
    for i in range(201):
        await db.execute(
            "INSERT INTO procedural_memory (id, task_type, principle, steps, tools_used, context_tags, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"id-{i}", "t", "p", "[]", "[]", "[]", "2026-01-01"),
        )
    await db.commit()
    assert await get_maturity_stage(db) == MaturityStage.MATURE


# ─── Operations ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_store_procedure(db):
    pid = await store_procedure(
        db, task_type="deploy", principle="always verify",
        steps=["build", "test", "deploy"],
        tools_used=["docker"], context_tags=["prod"],
    )
    assert len(pid) == 36  # UUID format


@pytest.mark.asyncio
async def test_record_success(db):
    pid = await store_procedure(
        db, task_type="t", principle="p", steps=["s"],
        tools_used=[], context_tags=[],
    )
    assert await record_success(db, pid) is True
    # Check values
    cursor = await db.execute("SELECT success_count, confidence, last_used FROM procedural_memory WHERE id = ?", (pid,))
    row = await cursor.fetchone()
    assert row[0] == 1  # success_count
    # Laplace: (1+1)/(1+0+2) = 2/3
    assert abs(row[1] - 2 / 3) < 1e-9
    assert row[2] is not None  # last_used set


@pytest.mark.asyncio
async def test_record_success_nonexistent(db):
    assert await record_success(db, "nonexistent") is False


@pytest.mark.asyncio
async def test_record_failure(db):
    pid = await store_procedure(
        db, task_type="t", principle="p", steps=["s"],
        tools_used=[], context_tags=[],
    )
    assert await record_failure(db, pid, condition="timeout") is True
    cursor = await db.execute("SELECT failure_count, failure_modes, confidence FROM procedural_memory WHERE id = ?", (pid,))
    row = await cursor.fetchone()
    assert row[0] == 1
    modes = json.loads(row[1])
    assert len(modes) == 1
    assert modes[0]["description"] == "timeout"
    assert modes[0]["times_hit"] == 1
    # Laplace: (0+1)/(0+1+2) = 1/3
    assert abs(row[2] - 1 / 3) < 1e-9


@pytest.mark.asyncio
async def test_record_failure_duplicate_increments(db):
    pid = await store_procedure(
        db, task_type="t", principle="p", steps=["s"],
        tools_used=[], context_tags=[],
    )
    await record_failure(db, pid, condition="timeout")
    await record_failure(db, pid, condition="timeout")
    cursor = await db.execute("SELECT failure_modes FROM procedural_memory WHERE id = ?", (pid,))
    row = await cursor.fetchone()
    modes = json.loads(row[0])
    assert len(modes) == 1
    assert modes[0]["times_hit"] == 2


@pytest.mark.asyncio
async def test_record_failure_nonexistent(db):
    assert await record_failure(db, "nonexistent", condition="x") is False


@pytest.mark.asyncio
async def test_record_workaround(db):
    pid = await store_procedure(
        db, task_type="t", principle="p", steps=["s"],
        tools_used=[], context_tags=[],
    )
    assert await record_workaround(
        db, pid, failed_method="curl", working_method="httpx", context="ssl issue"
    ) is True
    cursor = await db.execute("SELECT attempted_workarounds FROM procedural_memory WHERE id = ?", (pid,))
    row = await cursor.fetchone()
    wa = json.loads(row[0])
    assert len(wa) == 1
    assert wa[0]["description"] == "httpx"
    assert "curl" in wa[0]["outcome"]


@pytest.mark.asyncio
async def test_record_workaround_nonexistent(db):
    assert await record_workaround(db, "nonexistent", failed_method="a", working_method="b", context="c") is False


@pytest.mark.asyncio
async def test_update_confidence(db):
    pid = await store_procedure(
        db, task_type="t", principle="p", steps=["s"],
        tools_used=[], context_tags=[],
    )
    # Initial: s=0, f=0 -> (0+1)/(0+0+2) = 0.5
    conf = await update_confidence(db, pid)
    assert abs(conf - 0.5) < 1e-9

    await record_success(db, pid)
    await record_success(db, pid)
    # s=2, f=0 -> (2+1)/(2+0+2) = 3/4
    conf = await update_confidence(db, pid)
    assert abs(conf - 0.75) < 1e-9


@pytest.mark.asyncio
async def test_update_confidence_nonexistent(db):
    assert await update_confidence(db, "nonexistent") == 0.0


# ─── Matcher ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_failure_updates_last_used(db):
    pid = await store_procedure(
        db, task_type="t", principle="p", steps=["s"],
        tools_used=[], context_tags=[],
    )
    await record_failure(db, pid, condition="timeout")
    cursor = await db.execute("SELECT last_used FROM procedural_memory WHERE id = ?", (pid,))
    row = await cursor.fetchone()
    assert row[0] is not None


# ─── Matcher ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_find_best_match_returns_none_empty_context_tags(db):
    await store_procedure(
        db, task_type="deploy", principle="p", steps=["s"],
        tools_used=[], context_tags=["prod"],
    )
    assert await find_best_match(db, "deploy", []) is None


@pytest.mark.asyncio
async def test_find_best_match_returns_none_no_tag_overlap(db):
    await store_procedure(
        db, task_type="deploy", principle="p", steps=["s"],
        tools_used=[], context_tags=["prod"],
    )
    assert await find_best_match(db, "deploy", ["staging", "k8s"]) is None


@pytest.mark.asyncio
async def test_find_best_match_no_procedures(db):
    assert await find_best_match(db, "deploy", ["prod"]) is None


@pytest.mark.asyncio
async def test_find_best_match_single(db):
    pid = await store_procedure(
        db, task_type="deploy", principle="p", steps=["s"],
        tools_used=[], context_tags=["prod", "docker"],
    )
    await record_success(db, pid)
    match = await find_best_match(db, "deploy", ["prod", "docker"])
    assert match is not None
    assert match.procedure_id == pid
    assert match.task_type == "deploy"
    assert match.success_count == 1


@pytest.mark.asyncio
async def test_find_best_match_picks_higher_overlap(db):
    pid1 = await store_procedure(
        db, task_type="deploy", principle="p", steps=["s"],
        tools_used=[], context_tags=["prod"],
    )
    await record_success(db, pid1)
    pid2 = await store_procedure(
        db, task_type="deploy", principle="p", steps=["s"],
        tools_used=[], context_tags=["prod", "docker", "k8s"],
    )
    await record_success(db, pid2)
    # Query with ["prod", "docker"] — pid2 has better overlap (2/3 vs 1/2)
    # Both have same confidence after 1 success: 2/3
    match = await find_best_match(db, "deploy", ["prod", "docker"])
    assert match is not None
    assert match.procedure_id == pid2


@pytest.mark.asyncio
async def test_find_best_match_skips_deprecated(db):
    pid = await store_procedure(
        db, task_type="deploy", principle="p", steps=["s"],
        tools_used=[], context_tags=["prod"],
    )
    await record_success(db, pid)
    await db.execute("UPDATE procedural_memory SET deprecated = 1 WHERE id = ?", (pid,))
    await db.commit()
    assert await find_best_match(db, "deploy", ["prod"]) is None


@pytest.mark.asyncio
async def test_find_best_match_returns_failure_modes_and_workarounds(db):
    pid = await store_procedure(
        db, task_type="deploy", principle="p", steps=["s"],
        tools_used=[], context_tags=["prod"],
    )
    await record_success(db, pid)
    await record_failure(db, pid, condition="timeout")
    await record_workaround(db, pid, failed_method="curl", working_method="httpx", context="ssl")
    match = await find_best_match(db, "deploy", ["prod"])
    assert match is not None
    assert len(match.failure_modes) == 1
    assert len(match.workarounds) == 1
