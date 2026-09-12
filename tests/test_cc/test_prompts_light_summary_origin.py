"""WS-3: _fetch_prior_light_summary excludes external/unknown-origin content.

source="cc_reflection_light" and type="reflection_summary" are both free
observation_write params, so an external session can forge them. The summary's
content surfaces into the light-reflection prompt, so it must be origin-gated.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.cc.reflection_bridge._prompts import _fetch_prior_light_summary
from genesis.db.schema import create_all_tables, seed_data


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await create_all_tables(conn)
        await seed_data(conn)
        yield conn


async def _mk(db, oid, origin, content, when):
    await db.execute(
        "INSERT INTO observations (id, source, type, content, priority, "
        "created_at, origin_class) VALUES (?,?,?,?,?,?,?)",
        (oid, "cc_reflection_light", "reflection_summary", content, "low", when, origin),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_forged_external_light_summary_excluded(db):
    # A forged external row is the MOST RECENT (limit=1 DESC would pick it).
    await _mk(db, "ls_fp", "first_party", "legit summary", "2026-01-01T00:00:00")
    await _mk(db, "ls_ext", "external_untrusted", "INJECTED summary", "2026-06-01T00:00:00")

    out = await _fetch_prior_light_summary(db)
    assert out == "legit summary", out


@pytest.mark.asyncio
async def test_null_origin_light_summary_excluded(db):
    await _mk(db, "ls_null", None, "unknown-origin summary", "2026-06-01T00:00:00")
    out = await _fetch_prior_light_summary(db)
    assert out is None
