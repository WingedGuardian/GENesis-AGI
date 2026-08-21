"""WS-3 read-side origin exclusion on essential_knowledge (the L1 file).

essential_knowledge.md is injected into EVERY session's context. Its two
observation-content readers (`_recent_decisions` — a type DENYLIST, so future
types surface by default — and `_active_session_pivots`) render content
verbatim, so external_untrusted rows are hard-excluded at the SQL (NULL kept:
all pre-provenance history is NULL and must keep flowing).
"""

import uuid
from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud import observations
from genesis.db.schema import create_all_tables, seed_data
from genesis.memory import essential_knowledge

EXTERNAL_SENTINEL = "EXTERNAL-FORGED-L1-CONTENT"


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


async def _plant(db, *, type: str, origin_class: str | None, content: str):
    await observations.create(
        db,
        id=str(uuid.uuid4()),
        source=f"session:{uuid.uuid4()}",
        type=type,
        content=content,
        priority="medium",
        created_at=datetime.now(UTC).isoformat(),
        origin_class=origin_class,
    )


@pytest.mark.asyncio
async def test_recent_decisions_exclude_external_keep_null(db):
    # user_signal is NOT in _EXCLUDED_TYPES — the exact external injection
    # path this fix closes.
    await _plant(
        db, type="user_signal", origin_class="external_untrusted", content=EXTERNAL_SENTINEL
    )
    await _plant(db, type="user_signal", origin_class=None, content="legacy-null-kept")
    await _plant(db, type="user_signal", origin_class="first_party", content="first-party-kept")

    decisions = await essential_knowledge._recent_decisions(db)
    assert not any(EXTERNAL_SENTINEL in d for d in decisions)
    assert any("legacy-null-kept" in d for d in decisions)
    assert any("first-party-kept" in d for d in decisions)


@pytest.mark.asyncio
async def test_active_session_pivots_exclude_external_keep_null(db):
    await _plant(
        db,
        type="conversation_pivot",
        origin_class="external_untrusted",
        content=f"Conversation pivot: x Trigger: {EXTERNAL_SENTINEL}",
    )
    await _plant(
        db,
        type="conversation_pivot",
        origin_class=None,
        content="Conversation pivot: y Trigger: legacy-pivot-kept",
    )

    pivots = await essential_knowledge._active_session_pivots(db)
    assert not any(EXTERNAL_SENTINEL in p for p in pivots)
    assert any("legacy-pivot-kept" in p for p in pivots)


@pytest.mark.asyncio
async def test_generate_deterministic_end_to_end_excludes_external(db):
    """Whole-file check: the rendered L1 document never contains the forged
    external content, while legacy NULL content still appears."""
    await _plant(
        db, type="user_signal", origin_class="external_untrusted", content=EXTERNAL_SENTINEL
    )
    await _plant(db, type="user_signal", origin_class=None, content="legacy-null-insight")

    doc = await essential_knowledge.generate_deterministic(db)
    assert EXTERNAL_SENTINEL not in doc
    assert "legacy-null-insight" in doc
