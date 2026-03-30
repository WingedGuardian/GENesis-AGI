"""Tests for QuestionGate — rate limiter for user questions from reflection."""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import observations
from genesis.db.schema import create_all_tables, seed_data
from genesis.reflection.question_gate import QuestionGate


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


@pytest.fixture
def gate():
    return QuestionGate()


class TestCanAsk:
    @pytest.mark.asyncio
    async def test_can_ask_when_no_pending(self, db, gate):
        """Empty DB → can_ask returns True."""
        assert await gate.can_ask(db) is True

    @pytest.mark.asyncio
    async def test_can_ask_when_pending_exists(self, db, gate):
        """Pending question → can_ask returns False."""
        await gate.record_question(db, "What should we do?", "context here")
        assert await gate.can_ask(db) is False

    @pytest.mark.asyncio
    async def test_can_ask_after_resolved(self, db, gate):
        """Resolved question → can_ask returns True again."""
        obs_id = await gate.record_question(db, "What should we do?", "context")
        await gate.resolve_question(db, obs_id, "Do option A")
        assert await gate.can_ask(db) is True


class TestRecordQuestion:
    @pytest.mark.asyncio
    async def test_record_question_creates_observation(self, db, gate):
        """Recording creates type=pending_question observation."""
        obs_id = await gate.record_question(
            db, "Should we refactor?", "Code quality declining",
        )
        assert obs_id  # non-empty string

        # Verify in DB
        pending = await observations.query(
            db, type="pending_question", resolved=False, limit=10,
        )
        assert len(pending) == 1
        assert pending[0]["id"] == obs_id
        assert "Should we refactor?" in pending[0]["content"]
        assert pending[0]["source"] == "deep_reflection"
        assert pending[0]["priority"] == "high"


class TestResolveQuestion:
    @pytest.mark.asyncio
    async def test_resolve_question_marks_resolved(self, db, gate):
        """Resolving marks observation resolved=1."""
        obs_id = await gate.record_question(db, "Question?", "ctx")
        result = await gate.resolve_question(db, obs_id, "Answer here")
        assert result is True

        # Verify resolved
        obs = await observations.get_by_id(db, obs_id)
        assert obs["resolved"] == 1
        assert "User replied:" in obs["resolution_notes"]

    @pytest.mark.asyncio
    async def test_resolve_creates_response_observation(self, db, gate):
        """Resolving creates a type=question_response observation."""
        obs_id = await gate.record_question(db, "Question?", "ctx")
        await gate.resolve_question(db, obs_id, "My answer is X")

        responses = await observations.query(
            db, type="question_response", limit=10,
        )
        assert len(responses) == 1
        assert responses[0]["source"] == "user_reply"
        assert responses[0]["content"] == "My answer is X"

    @pytest.mark.asyncio
    async def test_resolve_nonexistent_returns_false(self, db, gate):
        """Resolving a non-existent ID returns False."""
        result = await gate.resolve_question(db, "nonexistent-id", "answer")
        assert result is False

        # No response observation created
        responses = await observations.query(
            db, type="question_response", limit=10,
        )
        assert len(responses) == 0
