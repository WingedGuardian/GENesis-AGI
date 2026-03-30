"""QuestionGate -- rate limiter for user questions from reflection.

Ensures max 1 pending question at any time. Questions are tracked as
observations (type='pending_question', resolved=False). A new question
is rejected if any pending question exists.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import aiosqlite

from genesis.db.crud import observations

logger = logging.getLogger(__name__)


class QuestionGate:
    """Rate-limits user questions from reflection to max 1 pending."""

    async def can_ask(self, db: aiosqlite.Connection) -> bool:
        """Return True if no pending questions exist."""
        pending = await observations.query(
            db, type="pending_question", resolved=False, limit=1,
        )
        return len(pending) == 0

    async def record_question(
        self,
        db: aiosqlite.Connection,
        question_text: str,
        context: str,
    ) -> str:
        """Record a pending question. Returns observation ID."""
        obs_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        await observations.create(
            db,
            id=obs_id,
            source="deep_reflection",
            type="pending_question",
            content=f"{question_text}\n\n---\nContext: {context}",
            priority="high",
            created_at=now,
        )
        return obs_id

    async def resolve_question(
        self,
        db: aiosqlite.Connection,
        obs_id: str,
        reply_text: str,
    ) -> bool:
        """Mark a pending question as resolved and store the response."""
        now = datetime.now(UTC).isoformat()
        resolved = await observations.resolve(
            db, obs_id,
            resolved_at=now,
            resolution_notes=f"User replied: {reply_text[:500]}",
        )
        if resolved:
            # Store response as separate observation for next reflection to see
            await observations.create(
                db,
                id=str(uuid.uuid4()),
                source="user_reply",
                type="question_response",
                content=reply_text,
                priority="high",
                created_at=now,
            )
        return resolved
