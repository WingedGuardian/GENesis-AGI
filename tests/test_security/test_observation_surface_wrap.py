"""WS-3 wrap-policy spot checks: external-origin observation content renders
inside <external-content> markers at the LLM-context surfaces, while
NULL-origin (unstamped internal/legacy) content renders unwrapped.

Sites under test here (the wrap set — the exclude set is covered by
test_context_gatherer_origin / test_essential_knowledge_origin):
genesis-ego observations section, sentinel diagnostic context, guardian
briefing, world snapshot. The remaining wrap sites share the same
one-liner (`wrap_if_external`) whose unit behavior is pinned in
tests/test_memory/test_provenance.py; the surface-coverage guardrail pins
that every wrap site actually calls it.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import observations
from genesis.db.schema import create_all_tables, seed_data

EXTERNAL_SENTINEL = "EXTERNAL-WRAPME-CONTENT"


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


async def _plant(db, *, origin_class, content, type="finding"):
    await observations.create(
        db,
        id=str(uuid.uuid4()),
        source="spot_check",
        type=type,
        content=content,
        priority="high",
        created_at=datetime.now(UTC).isoformat(),
        origin_class=origin_class,
    )


def _assert_wrapped(text: str, sentinel: str):
    """sentinel appears, and appears inside external-content markers."""
    assert sentinel in text
    before = text.split(sentinel, 1)[0]
    assert "<external-content" in before, "sentinel not preceded by an open marker"
    after = text.split(sentinel, 1)[1]
    assert "</external-content>" in after, "sentinel not followed by a close marker"


@pytest.mark.asyncio
async def test_genesis_ego_observations_section_wraps_external(db):
    from genesis.ego.genesis_context import GenesisEgoContextBuilder

    await _plant(db, origin_class="external_untrusted", content=EXTERNAL_SENTINEL)
    await _plant(db, origin_class=None, content="plain-internal-item")

    builder = GenesisEgoContextBuilder(db=db, health_data=AsyncMock(), capabilities={})
    section = await builder._observations_section()
    _assert_wrapped(section, EXTERNAL_SENTINEL)
    assert "plain-internal-item" in section
    # NULL-origin content is NOT wrapped
    assert (
        "plain-internal-item"
        not in section.split("<external-content", 1)[-1].split("</external-content>")[0]
    )


@pytest.mark.asyncio
async def test_sentinel_context_wraps_external(db):
    from genesis.sentinel.context import assemble_diagnostic_context

    await _plant(db, origin_class="external_untrusted", content=EXTERNAL_SENTINEL)
    ctx = await assemble_diagnostic_context(
        alarms=[], trigger_source="test", trigger_reason="spot-check", db=db
    )
    _assert_wrapped(ctx, EXTERNAL_SENTINEL)


@pytest.mark.asyncio
async def test_guardian_briefing_wraps_external(db):
    from genesis.guardian.briefing import build_dynamic_briefing

    await _plant(db, origin_class="external_untrusted", content=EXTERNAL_SENTINEL)
    await _plant(db, origin_class="first_party", content="trusted-briefing-item")
    content = await build_dynamic_briefing(db)
    joined = "\n".join(content.active_observations)
    _assert_wrapped(joined, EXTERNAL_SENTINEL)
    assert "trusted-briefing-item" in joined


@pytest.mark.asyncio
async def test_world_snapshot_wraps_external_user_signals(db):
    from genesis.ego.world_snapshot import build

    await _plant(
        db, origin_class="external_untrusted", content=EXTERNAL_SENTINEL, type="user_signal"
    )
    await _plant(db, origin_class=None, content="null-signal-plain", type="user_signal")
    snapshot = await build(db)
    md = snapshot.render()
    _assert_wrapped(md, EXTERNAL_SENTINEL)
    assert "null-signal-plain" in md


# ── F2: forgeable-pin content-into-prompt surfaces now EXCLUDE external ──────


@pytest.mark.asyncio
async def test_user_ego_escalations_section_excludes_external(db):
    """type='escalation_to_user_ego' is forgeable via observation_write and its
    content lands in the user-ego (CEO) prompt at priority=critical — an
    external-origin forgery must not appear; NULL/first-party escalations do."""
    from genesis.ego.user_context import UserEgoContextBuilder

    await _plant(db, origin_class="external_untrusted", content=EXTERNAL_SENTINEL,
                 type="escalation_to_user_ego")
    await _plant(db, origin_class=None, content="legit-internal-escalation",
                 type="escalation_to_user_ego")

    builder = UserEgoContextBuilder(db=db)
    section = await builder._genesis_escalations_section()
    assert EXTERNAL_SENTINEL not in section
    assert "legit-internal-escalation" in section
