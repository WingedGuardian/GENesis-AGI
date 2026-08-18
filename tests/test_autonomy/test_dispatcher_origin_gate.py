"""WS-3 poisoning gate on the autonomy dispatcher's observation pickup.

A ``task_detected`` observation forged via observation_write from an
external-origin session must never spawn a background CC session. The pickup
path is currently INERT (the observations table has no ``metadata`` column, so
``plan_path`` is always None and every row skips there) — this gate is
forward-safe: it ensures that a future ``metadata`` addition cannot silently
re-arm an ungated auto-dispatch surface. A barred row is resolved
(``barred:untrusted_origin``) rather than left to re-log every cycle.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import aiosqlite
import pytest

from genesis.autonomy.dispatcher import TaskDispatcher
from genesis.db.crud import observations


async def _seed(db, *, id: str, origin_class: str | None) -> None:
    await observations.create(
        db,
        id=id,
        source="inbox_evaluation" if origin_class == "external_untrusted" else "conversation",
        type="task_detected",
        content=f"task-{id}",
        priority="medium",
        created_at="2026-03-08T00:00:00",
        origin_class=origin_class,
    )


async def _dispatcher_with_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    from genesis.db.schema import create_all_tables

    await create_all_tables(db)
    await db.commit()
    # executor is never invoked: Path 1 is a no-op (no active tasks) and the
    # gate/plan_path checks short-circuit before self.submit for every row here.
    return db, TaskDispatcher(db=db, executor=MagicMock())


@pytest.mark.asyncio
async def test_untrusted_task_detected_barred_and_resolved():
    db, disp = await _dispatcher_with_db()
    try:
        await _seed(db, id="ext", origin_class="external_untrusted")
        await _seed(db, id="nul", origin_class=None)  # fail-closed

        n = await disp.dispatch_cycle()
        assert n == 0  # nothing dispatched (inert)

        pending = {
            r["id"] for r in await observations.query(db, type="task_detected", resolved=False)
        }
        assert "ext" not in pending  # barred → resolved
        assert "nul" not in pending  # NULL fail-closed → barred → resolved

        resolved = {
            r["id"]: r for r in await observations.query(db, type="task_detected", resolved=True)
        }
        assert "barred:untrusted_origin" in (resolved["ext"].get("resolution_notes") or "")
        assert "barred:untrusted_origin" in (resolved["nul"].get("resolution_notes") or "")
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_trusted_task_detected_not_barred():
    """A first_party task_detected is NOT barred by the gate — it proceeds to
    the (inert) plan_path check and is skipped there, left pending. Proves the
    gate discriminates on origin, not on everything."""
    db, disp = await _dispatcher_with_db()
    try:
        await _seed(db, id="tru", origin_class="first_party")
        n = await disp.dispatch_cycle()
        assert n == 0  # still inert (no plan_path)

        pending = {
            r["id"] for r in await observations.query(db, type="task_detected", resolved=False)
        }
        assert "tru" in pending  # skipped at plan_path, NOT barred → still pending
    finally:
        await db.close()
