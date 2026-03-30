"""Tests for FindingsBridge — code audit findings to surplus_insights staging."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db import schema
from genesis.surplus.findings_bridge import FindingsBridge


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        for ddl in schema.TABLES.values():
            await conn.execute(ddl)
        await conn.commit()
        yield conn


def _make_insight(
    severity: str = "medium",
    file: str = "foo.py",
    line: int = 10,
    description: str = "Something wrong",
    suggestion: str = "Fix it",
    confidence: float = 0.9,
    model: str = "test-model",
) -> dict:
    return {
        "severity": severity,
        "file": file,
        "line": line,
        "description": description,
        "suggestion": suggestion,
        "confidence": confidence,
        "model": model,
    }


async def _get_surplus_rows(db) -> list[dict]:
    cursor = await db.execute("SELECT * FROM surplus_insights")
    return [dict(r) for r in await cursor.fetchall()]


class TestFindingsBridge:
    @pytest.mark.asyncio
    async def test_new_findings_written(self, db):
        bridge = FindingsBridge(db)
        insights = [_make_insight(), _make_insight(file="bar.py")]
        count = await bridge.bridge_findings(insights)
        assert count == 2
        rows = await _get_surplus_rows(db)
        assert len(rows) == 2
        assert rows[0]["source_task_type"] == "code_audit"

    @pytest.mark.asyncio
    async def test_duplicate_findings_skipped(self, db):
        bridge = FindingsBridge(db)
        insight = _make_insight()
        assert await bridge.bridge_findings([insight]) == 1
        assert await bridge.bridge_findings([insight]) == 0
        rows = await _get_surplus_rows(db)
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_empty_insights_returns_zero(self, db):
        bridge = FindingsBridge(db)
        assert await bridge.bridge_findings([]) == 0

    @pytest.mark.asyncio
    async def test_malformed_insight_does_not_crash(self, db):
        bridge = FindingsBridge(db)
        count = await bridge.bridge_findings([{}, {"severity": "low"}])
        assert count == 0  # Filtered by confidence gate

    @pytest.mark.asyncio
    async def test_low_confidence_findings_filtered(self, db):
        bridge = FindingsBridge(db)
        low = _make_insight(confidence=0.5, file="low.py")
        high = _make_insight(confidence=0.9, file="high.py")
        count = await bridge.bridge_findings([low, high])
        assert count == 1
        rows = await _get_surplus_rows(db)
        assert len(rows) == 1
        assert "high.py" in rows[0]["content"]

    @pytest.mark.asyncio
    async def test_generating_model_stored(self, db):
        bridge = FindingsBridge(db)
        await bridge.bridge_findings([_make_insight(model="kimi-k2.5")])
        rows = await _get_surplus_rows(db)
        assert rows[0]["generating_model"] == "kimi-k2.5"
