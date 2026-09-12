"""Unit tests for ``ego_crud.list_directives`` — the status-filtered directive
list the dashboard's Directives panel reads.

Covers: status filtering, optional ``ego_target`` scoping, the ``kind='directive'``
default that structurally excludes decision rows, and the
``COALESCE(resolved_at, created_at) DESC`` ordering (verified per homogeneous
call). Timestamps are inserted explicitly so ordering never depends on the wall
clock.
"""

from __future__ import annotations

import pytest

from genesis.db.crud import ego as ego_crud

pytestmark = pytest.mark.asyncio


async def _mk_directive(
    db,
    *,
    id,
    ego_target="user_ego",
    status="active",
    created_at,
    resolved_at=None,
    kind="directive",
    priority="high",
):
    await db.execute(
        """INSERT INTO ego_directives
           (id, content, priority, source, ego_target, status, created_at,
            resolved_at, kind)
           VALUES (?, ?, ?, 'user', ?, ?, ?, ?, ?)""",
        (id, f"directive {id}", priority, ego_target, status, created_at, resolved_at, kind),
    )
    await db.commit()


async def test_active_only_both_targets(db):
    await _mk_directive(db, id="ua", ego_target="user_ego", created_at="2026-07-01T00:00:00+00:00")
    await _mk_directive(
        db, id="ga", ego_target="genesis_ego", created_at="2026-07-02T00:00:00+00:00"
    )
    await _mk_directive(
        db,
        id="done",
        status="completed",
        created_at="2026-06-01T00:00:00+00:00",
        resolved_at="2026-06-02T00:00:00+00:00",
    )

    rows = await ego_crud.list_directives(db, statuses=("active",))
    ids = [r["id"] for r in rows]
    assert ids == ["ga", "ua"]  # active only, newest created_at first (COALESCE)


async def test_ego_target_filter(db):
    await _mk_directive(db, id="ua", ego_target="user_ego", created_at="2026-07-01T00:00:00+00:00")
    await _mk_directive(
        db, id="ga", ego_target="genesis_ego", created_at="2026-07-02T00:00:00+00:00"
    )

    rows = await ego_crud.list_directives(db, ego_target="genesis_ego", statuses=("active",))
    assert [r["id"] for r in rows] == ["ga"]


async def test_default_kind_excludes_decisions(db):
    await _mk_directive(db, id="dir", created_at="2026-07-01T00:00:00+00:00")
    await _mk_directive(db, id="dec", kind="decision", created_at="2026-07-02T00:00:00+00:00")

    rows = await ego_crud.list_directives(db, statuses=("active",))
    assert [r["id"] for r in rows] == ["dir"]  # decision row excluded by default kind


async def test_resolved_ordering_newest_resolved_first(db):
    await _mk_directive(
        db,
        id="r_old",
        status="completed",
        created_at="2026-06-01T00:00:00+00:00",
        resolved_at="2026-06-05T00:00:00+00:00",
    )
    await _mk_directive(
        db,
        id="r_new",
        status="cancelled",
        created_at="2026-05-01T00:00:00+00:00",
        resolved_at="2026-07-10T00:00:00+00:00",
    )
    rows = await ego_crud.list_directives(db, statuses=("completed", "cancelled"))
    # ordered by resolved_at DESC even though r_new was created earlier
    assert [r["id"] for r in rows] == ["r_new", "r_old"]


async def test_limit_is_respected(db):
    for i in range(5):
        await _mk_directive(db, id=f"d{i}", created_at=f"2026-07-0{i + 1}T00:00:00+00:00")
    rows = await ego_crud.list_directives(db, statuses=("active",), limit=2)
    assert len(rows) == 2
    assert rows[0]["id"] == "d4"  # newest first, capped at 2


async def test_empty_statuses_returns_empty(db):
    await _mk_directive(db, id="a", created_at="2026-07-01T00:00:00+00:00")
    assert await ego_crud.list_directives(db, statuses=()) == []
