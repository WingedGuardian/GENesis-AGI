"""WS-M PR-2: staged-idea → follow_ups 'idea' review-lane promotion pass.

Covers list_promotable_ideas (FIFO / task-type filter / ttl-unexpired), the GC
promotion pass (promote → kind='idea', mark surplus promoted, idempotency, the
settings off-switch, IDEA-only), the config lever, dispatch-exclusion of the
'idea' kind, and the settings validator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import genesis.db.crud.follow_ups as fu_crud
import genesis.db.crud.surplus as surplus_crud
from genesis.surplus.jobs.gates import _promote_staged_ideas


async def _stage(db, *, id, task_type, created_at, ttl, content="an idea"):
    await surplus_crud.create(
        db,
        id=id,
        content=content,
        source_task_type=task_type,
        generating_model="m",
        drive_alignment="competence",
        created_at=created_at,
        ttl=ttl,
        confidence=0.6,
    )


def _t(days_from_now: int) -> str:
    return (datetime.now(UTC) + timedelta(days=days_from_now)).isoformat()


# ---------------------------------------------------------------------------
# list_promotable_ideas
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_promotable_ideas_fifo_and_filters(db):
    # Two idea rows (different ages) + one non-idea + one expired idea.
    await _stage(db, id="i-new", task_type="brainstorm_self", created_at=_t(-1), ttl=_t(7))
    await _stage(db, id="i-old", task_type="brainstorm_user", created_at=_t(-5), ttl=_t(7))
    await _stage(db, id="not-idea", task_type="gap_clustering", created_at=_t(-3), ttl=_t(7))
    await _stage(db, id="i-expired", task_type="meta_brainstorm", created_at=_t(-9), ttl=_t(-1))

    rows = await surplus_crud.list_promotable_ideas(
        db,
        task_types=["brainstorm_self", "brainstorm_user", "meta_brainstorm"],
        limit=10,
    )
    ids = [r["id"] for r in rows]
    # FIFO (oldest created_at first); non-idea + expired excluded.
    assert ids == ["i-old", "i-new"]


@pytest.mark.asyncio
async def test_list_promotable_ideas_respects_limit_and_empty_types(db):
    for i in range(3):
        await _stage(db, id=f"i{i}", task_type="brainstorm_self", created_at=_t(-i), ttl=_t(7))
    assert (
        len(await surplus_crud.list_promotable_ideas(db, task_types=["brainstorm_self"], limit=2))
        == 2
    )
    assert await surplus_crud.list_promotable_ideas(db, task_types=[], limit=10) == []


# ---------------------------------------------------------------------------
# _promote_staged_ideas (the GC pass)
# ---------------------------------------------------------------------------


async def _ideas_in_lane(db) -> list[dict]:
    cur = await db.execute("SELECT * FROM follow_ups WHERE kind = 'idea'")
    cur.row_factory = None
    rows = await cur.fetchall()
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]


@pytest.mark.asyncio
async def test_promote_creates_idea_follow_up_and_marks_promoted(db):
    await _stage(
        db,
        id="sid1",
        task_type="brainstorm_self",
        created_at=_t(-1),
        ttl=_t(7),
        content="Dynamic Awareness Depth Tuning",
    )

    n = await _promote_staged_ideas(db)

    assert n == 1
    lane = await _ideas_in_lane(db)
    assert len(lane) == 1
    fu = lane[0]
    assert fu["kind"] == "idea"
    assert fu["source"] == "surplus_ideation"
    assert fu["strategy"] == "surplus_task"
    assert fu["domain"] == "internal"
    assert "Awareness Depth" in fu["content"]
    # surplus row flipped out of pending.
    staged = await surplus_crud.get_by_id(db, "sid1")
    assert staged["promotion_status"] == "promoted"
    assert staged["promoted_to"] == f"follow_up:{fu['id']}"


@pytest.mark.asyncio
async def test_promote_is_idempotent(db):
    await _stage(db, id="sid1", task_type="brainstorm_self", created_at=_t(-1), ttl=_t(7))
    assert await _promote_staged_ideas(db) == 1
    # Second pass: row no longer pending → 0 new, still exactly one lane row.
    assert await _promote_staged_ideas(db) == 0
    assert len(await _ideas_in_lane(db)) == 1


@pytest.mark.asyncio
async def test_promote_reconciles_orphaned_followup(db):
    """A crash between create and promote leaves a pending row whose follow_up
    already exists; the next pass reconciles (promotes) without a duplicate."""
    import hashlib

    await _stage(db, id="sid1", task_type="brainstorm_self", created_at=_t(-1), ttl=_t(7))
    dedup_key = hashlib.sha256(b"surplus_ideation|sid1").hexdigest()
    await fu_crud.create(
        db,
        content="pre-existing",
        source="surplus_ideation",
        strategy="surplus_task",
        kind="idea",
        domain="internal",
        dedup_key=dedup_key,
    )
    # surplus row still pending (promote never ran).
    n = await _promote_staged_ideas(db)
    assert n == 0  # not counted as a new promotion
    assert len(await _ideas_in_lane(db)) == 1  # no duplicate
    staged = await surplus_crud.get_by_id(db, "sid1")
    assert staged["promotion_status"] == "promoted"  # reconciled


@pytest.mark.asyncio
async def test_promote_only_idea_types_not_self_observations(db):
    await _stage(db, id="obs1", task_type="gap_clustering", created_at=_t(-1), ttl=_t(7))
    assert await _promote_staged_ideas(db) == 0
    assert await _ideas_in_lane(db) == []
    # self-obs staged row untouched (still pending).
    assert (await surplus_crud.get_by_id(db, "obs1"))["promotion_status"] == "pending"


@pytest.mark.asyncio
async def test_promote_disabled_by_env(db, monkeypatch):
    monkeypatch.setenv("GENESIS_SURPLUS_IDEATION_PROMOTION_DISABLED", "1")
    await _stage(db, id="sid1", task_type="brainstorm_self", created_at=_t(-1), ttl=_t(7))
    assert await _promote_staged_ideas(db) == 0
    assert await _ideas_in_lane(db) == []


@pytest.mark.asyncio
async def test_promote_respects_cap(db, monkeypatch):
    monkeypatch.setattr("genesis.surplus.promotion_config.cap_per_run", lambda: 2)
    for i in range(3):
        await _stage(db, id=f"s{i}", task_type="brainstorm_self", created_at=_t(-i - 1), ttl=_t(7))
    assert await _promote_staged_ideas(db) == 2
    assert len(await _ideas_in_lane(db)) == 2


@pytest.mark.asyncio
async def test_promoted_idea_excluded_from_dispatch(db):
    """The 'idea' kind must never be auto-dispatched (positive kind='follow_up'
    allow-list in get_actionable / get_pending)."""
    await _stage(db, id="sid1", task_type="brainstorm_self", created_at=_t(-1), ttl=_t(7))
    await _promote_staged_ideas(db)
    actionable = await fu_crud.get_actionable(db)
    assert all(f["kind"] == "follow_up" for f in actionable)
    assert not any(f["source"] == "surplus_ideation" for f in actionable)


# ---------------------------------------------------------------------------
# config lever + settings validator
# ---------------------------------------------------------------------------


def test_config_defaults_and_env_kill(monkeypatch):
    from genesis.surplus import promotion_config as pc

    assert pc.is_enabled() is True  # shipped default
    assert pc.cap_per_run() == 20
    monkeypatch.setenv("GENESIS_SURPLUS_IDEATION_PROMOTION_DISABLED", "1")
    assert pc.is_enabled() is False


def test_settings_validator():
    from genesis.mcp.health.settings import _validate_surplus_ideation_promotion as v

    assert v({"enabled": True, "cap_per_run": 5}) == []
    assert v({"enabled": "false"})  # truthy string rejected
    assert v({"cap_per_run": 0})  # non-positive rejected
    assert v({"cap_per_run": -3})
    assert v({"bogus": 1})  # unknown key rejected
