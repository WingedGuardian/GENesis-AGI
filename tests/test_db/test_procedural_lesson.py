"""The lesson columns on procedural_memory: storage, migration and listing."""

import json

from genesis.db.crud import procedural

_COMMON = dict(
    task_type="deploy",
    principle="launch deploys detached",
    steps=["detach it"],
    tools_used=["Bash"],
    context_tags=["deploy"],
    created_at="2026-01-01T00:00:00",
)
_SPEC = {"patterns": [r"\bupdate\.sh\b"], "repeat": "session"}
_LESSON = {
    "authored_via": "mcp",
    "authored_in": "foreground",
    "origin": "first_party",
    "reviewed": False,
    "authored_at": "2026-10-13T00:00:00+00:00",
}


async def _columns(db) -> set[str]:
    cursor = await db.execute("PRAGMA table_info(procedural_memory)")
    return {row[1] for row in await cursor.fetchall()}


async def test_create_round_trips_both_columns(db):
    await procedural.create(db, id="l1", trigger_spec=_SPEC, lesson=_LESSON, **_COMMON)
    row = await procedural.get_by_id(db, "l1")
    assert json.loads(row["trigger_spec"]) == _SPEC
    assert json.loads(row["lesson"]) == _LESSON


async def test_an_ordinary_row_leaves_both_columns_null(db):
    await procedural.create(db, id="p1", **_COMMON)
    row = await procedural.get_by_id(db, "p1")
    assert row["trigger_spec"] is None
    assert row["lesson"] is None


async def test_update_serializes_both_columns(db):
    await procedural.create(db, id="l2", **_COMMON)
    await procedural.update(db, "l2", trigger_spec=_SPEC, lesson=_LESSON)
    row = await procedural.get_by_id(db, "l2")
    assert json.loads(row["trigger_spec"]) == _SPEC
    assert json.loads(row["lesson"]) == _LESSON


async def test_upsert_writes_both_and_keeps_them_when_omitted(db):
    await procedural.upsert(db, id="l3", trigger_spec=_SPEC, lesson=_LESSON, **_COMMON)
    # A later upsert that does not carry them must not wipe them.
    await procedural.upsert(db, id="l3", **_COMMON)
    row = await procedural.get_by_id(db, "l3")
    assert json.loads(row["trigger_spec"]) == _SPEC
    assert json.loads(row["lesson"]) == _LESSON
    # One that does carry them replaces them.
    reviewed = {**_LESSON, "reviewed": True}
    await procedural.upsert(db, id="l3", lesson=reviewed, **_COMMON)
    row = await procedural.get_by_id(db, "l3")
    assert json.loads(row["lesson"])["reviewed"] is True


async def test_list_lessons_is_unbounded_and_skips_deprecated(db):
    for i in range(105):
        await procedural.create(db, id=f"lesson-{i}", lesson=_LESSON, **_COMMON)
    await procedural.create(db, id="earned", **_COMMON)
    await procedural.create(db, id="retired", lesson=_LESSON, **_COMMON)
    await procedural.update(db, "retired", deprecated=1)
    await procedural.create(db, id="held", lesson=_LESSON, **_COMMON)
    await procedural.quarantine(db, "held")
    ids = {row["id"] for row in await procedural.list_lessons(db)}
    assert len(ids) == 105
    assert not {"earned", "retired", "held"} & ids
    every = {row["id"] for row in await procedural.list_lessons(db, include_quarantined=True)}
    assert every == ids | {"held"}


async def test_the_migration_adds_the_columns_to_an_older_table(db):
    from genesis.db.schema._migrations import _migrate_add_columns

    await db.execute("ALTER TABLE procedural_memory DROP COLUMN trigger_spec")
    await db.execute("ALTER TABLE procedural_memory DROP COLUMN lesson")
    assert not {"trigger_spec", "lesson"} & await _columns(db)
    await _migrate_add_columns(db)
    assert {"trigger_spec", "lesson"} <= await _columns(db)
    # Idempotent: a second run leaves the table as it is.
    await _migrate_add_columns(db)
    assert {"trigger_spec", "lesson"} <= await _columns(db)
