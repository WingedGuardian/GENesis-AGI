"""WS-3 poisoning gate on the autonomy dispatcher's observation pickup.

A ``task_detected`` observation forged via observation_write from an
external-origin session must never spawn a background CC session. The pickup
path is currently INERT (the observations table has no ``metadata`` column, so
``plan_path`` is always None and every row skips there) — this gate is
forward-safe: it ensures that a future ``metadata`` addition cannot silently
re-arm an ungated auto-dispatch surface. A barred row is SKIP-ONLY: it is not
dispatched but is left PENDING (not resolved), so legitimate NULL-origin owner
rows stay visible in the dashboard/L1 exactly as before — only dispatch is
refused. (Resolving barred rows would hide those legitimate rows; producer-side
origin stamping is tracked in the memory-provenance follow-up.)
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
async def test_untrusted_task_detected_not_dispatched_and_left_pending():
    """SKIP-ONLY: an untrusted-origin task_detected is not dispatched, and — the
    anti-regression property — is NOT resolved, so it stays visible in the
    dashboard/L1 exactly as before this gate; only dispatch is refused. The gate
    is a no-op today (inert pickup path), so the observable guarantee is
    'nothing dispatched, nothing hidden'."""
    db, disp = await _dispatcher_with_db()
    try:
        await _seed(db, id="ext", origin_class="external_untrusted")
        await _seed(db, id="nul", origin_class=None)  # fail-closed

        n = await disp.dispatch_cycle()
        assert n == 0  # nothing dispatched

        pending = {
            r["id"] for r in await observations.query(db, type="task_detected", resolved=False)
        }
        assert "ext" in pending  # skip-only: NOT resolved/hidden
        assert "nul" in pending
        # Nothing was resolved by the dispatcher — no visibility regression.
        resolved = await observations.query(db, type="task_detected", resolved=True)
        assert resolved == []
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_trusted_task_detected_also_left_pending():
    """A first_party task_detected is likewise not dispatched today (inert
    plan_path) and stays pending — same observable outcome as the untrusted case
    while the path is inert. Producer-side stamping + live dispatch behaviour are
    tracked in the memory-provenance follow-up."""
    db, disp = await _dispatcher_with_db()
    try:
        await _seed(db, id="tru", origin_class="first_party")
        n = await disp.dispatch_cycle()
        assert n == 0

        pending = {
            r["id"] for r in await observations.query(db, type="task_detected", resolved=False)
        }
        assert "tru" in pending
    finally:
        await db.close()
