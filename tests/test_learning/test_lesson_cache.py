"""Lessons in the trigger cache, the promoter and per-prompt surfacing (T-3)."""

from __future__ import annotations

import json

import pytest

from genesis.db.crud import procedural
from genesis.learning.procedural import promoter, trigger_cache
from genesis.learning.procedural.embedding import pack_embedding
from genesis.learning.procedural.lesson_shape import LIVE_LESSON_SQL, is_live_lesson
from genesis.memory import proactive

_NOW = "2026-10-14T00:00:00+00:00"


def _lesson(origin: str = "owner", reviewed: bool = False) -> dict:
    return {
        "authored_via": "mcp",
        "authored_in": "foreground",
        "origin": origin,
        "reviewed": reviewed,
        "authored_at": _NOW,
    }


async def _row(db, pid: str, *, tier="DORMANT", lesson=None, trigger_spec=None, embed=False) -> str:
    return await procedural.create(
        db,
        id=pid,
        task_type=pid,
        principle=f"principle {pid}",
        steps=["step"],
        tools_used=["Bash"],
        context_tags=["tag-" + pid],
        created_at=_NOW,
        draft=0,
        activation_tier=tier,
        tool_trigger=["Bash"],
        confidence=0.9,
        lesson=lesson,
        trigger_spec=trigger_spec,
        principle_embedding=pack_embedding([1.0] + [0.0] * 1023) if embed else None,
    )


@pytest.fixture(autouse=True)
def cache_path(tmp_path, monkeypatch):
    path = tmp_path / "procedure_triggers.yaml"
    monkeypatch.setattr(trigger_cache, "_CACHE_PATH", path)
    return path


def _entries(cache_path) -> dict[str, dict]:
    data = json.loads(cache_path.with_suffix(".json").read_text())
    return {t["procedure_id"]: t for t in data["triggers"]}


@pytest.mark.parametrize(
    ("raw", "live"),
    [
        (None, None),  # no lesson: not a lesson at all (SQL passes it)
        (_lesson("owner"), True),
        (_lesson("first_party"), True),
        (_lesson("external_untrusted"), False),
        (_lesson("external_untrusted", reviewed=True), True),
        ({**_lesson("external_untrusted"), "reviewed": 1}, False),
        ({"origin": "somewhere"}, False),
        ("not json", False),
        ([1, 2], False),
        ('"owner"', False),
        # Duplicate keys: SQLite reads the first, Python the last; both hold it.
        ('{"origin": "owner", "origin": "external_untrusted"}', False),
        ('{"origin": "external_untrusted", "reviewed": true, "reviewed": false}', False),
        ('{"origin": "external_untrusted", "reviewed": NaN}', False),
        # SQLite's JSON functions stop reading at a NUL; Python rejects the tail.
        (json.dumps(_lesson("owner")) + "\x00junk", False),
        # A BLOB: SQLite's JSON functions would read it, Python never does.
        (json.dumps(_lesson("owner")).encode(), False),
        # Only a complete record is read for its origin and review state.
        ({"origin": "owner"}, False),
        ({"origin": "external_untrusted", "reviewed": True}, False),
        ({**_lesson("owner"), "extra": 1}, False),
        ({**_lesson("external_untrusted", reviewed=True), "origin": None}, False),
        ({**_lesson("owner"), "authored_via": None}, False),
        ({**_lesson("owner"), "authored_in": "elsewhere"}, False),
        ({**_lesson("owner"), "reviewed": "yes"}, False),
        ({**_lesson("owner"), "authored_at": 5}, False),
        ({**_lesson("owner"), "origin": 1}, False),
    ],
)
async def test_python_and_sql_agree_on_liveness(db, raw, live):
    stored = raw if raw is None or isinstance(raw, str | bytes) else json.dumps(raw)
    if raw is not None:
        assert is_live_lesson(stored) is live
    rows = await db.execute_fetchall(
        f"SELECT ({LIVE_LESSON_SQL}) FROM (SELECT ? AS lesson)", (stored,)
    )
    assert bool(rows[0][0]) is (True if live is None else live)


async def test_cache_holds_live_lessons_and_leaves_held_ones_out(db, cache_path):
    await _row(db, "held", tier="ADVISORY", lesson=_lesson("external_untrusted"))
    await _row(db, "reviewed", lesson=_lesson("external_untrusted", reviewed=True))
    await _row(
        db, "owned", lesson=_lesson("owner"), trigger_spec={"patterns": ["ssh"], "repeat": "every"}
    )
    await _row(
        db, "earned", tier="CORE", trigger_spec={"patterns": [r"git\s+push"], "repeat": "session"}
    )
    await trigger_cache.regenerate(db)
    entries = _entries(cache_path)

    assert "held" not in entries  # held even though its tier is ADVISORY
    assert entries["reviewed"]["kind"] == "lesson"
    assert entries["reviewed"]["patterns"] == ["tag-reviewed"]
    assert entries["reviewed"]["repeat"] == "session"
    assert "label" not in entries["reviewed"]
    assert entries["owned"]["patterns"] == ["ssh"]
    assert entries["owned"]["repeat"] == "every"
    assert entries["owned"]["label"] == "unreviewed"
    # The deployed advisor matches on context_patterns; lessons carry none.
    assert "context_patterns" not in entries["owned"]
    # An earned row with a trigger_spec uses its patterns, and keeps the old key.
    assert entries["earned"]["kind"] == "earned"
    assert entries["earned"]["patterns"] == [r"git\s+push"]
    assert entries["earned"]["repeat"] == "every"
    assert entries["earned"]["context_patterns"] == ["tag-earned"]


async def test_every_live_lesson_reaches_the_cache(db, cache_path):
    for i in range(150):
        await _row(db, f"l{i:03d}", lesson=_lesson("owner"))
    assert await trigger_cache.regenerate(db) == 150


async def test_promoter_leaves_a_lesson_tier_alone(db):
    await _row(db, "lesson", lesson=_lesson("owner"))
    await _row(db, "control")
    for pid in ("lesson", "control"):
        await procedural.update(db, pid, invocation_count=100, success_count=5, confidence=0.9)
    await promoter.promote_and_demote(db)
    assert (await procedural.get_by_id(db, "control"))["activation_tier"] != "DORMANT"
    assert (await procedural.get_by_id(db, "lesson"))["activation_tier"] == "DORMANT"


async def test_proactive_surfacing_skips_held_lessons(db, monkeypatch):
    monkeypatch.setattr(proactive, "_procedure_cache", None)
    await _row(db, "held", lesson=_lesson("external_untrusted"), embed=True)
    await _row(db, "reviewed", lesson=_lesson("external_untrusted", reviewed=True), embed=True)
    cache = await proactive._rebuild_procedure_cache(db)
    ids = {meta[0] for meta in cache.meta}
    assert "held" not in ids
    assert "reviewed" in ids


async def test_session_start_injection_skips_held_lessons(tmp_path):
    import aiosqlite

    from genesis.db.schema import create_all_tables
    from genesis.learning.procedural.session_inject import load_active_procedures

    path = tmp_path / "t.db"
    async with aiosqlite.connect(str(path)) as conn:
        await create_all_tables(conn)
        await _row(conn, "held", tier="CORE", lesson=_lesson("external_untrusted"))
        await _row(conn, "owned", tier="CORE", lesson=_lesson("owner"))
        await conn.commit()
    text = await load_active_procedures(path)
    assert "principle owned" in text
    assert "principle held" not in text


async def test_one_malformed_row_does_not_stop_the_cache(db, cache_path):
    await _row(db, "good", lesson=_lesson("owner"))
    await _row(db, "bad", lesson=_lesson("owner"))
    await _row(db, "bad_earned", tier="CORE")
    await db.execute(
        "UPDATE procedural_memory SET tool_trigger = 'Bash' WHERE id IN ('bad', 'bad_earned')"
    )
    assert await trigger_cache.regenerate(db) == 1
    assert set(_entries(cache_path)) == {"good"}


async def test_the_live_recheck_drops_a_lesson_held_since_the_cache_was_built(db, monkeypatch):
    monkeypatch.setattr(proactive, "_procedure_cache", None)
    await _row(
        db, "lesson", tier="CORE", lesson=_lesson("external_untrusted", reviewed=True), embed=True
    )
    query = [1.0] + [0.0] * 1023
    assert (await proactive._surface_procedure(db, query)) is not None
    held = json.dumps(_lesson("external_untrusted"))
    await db.execute("UPDATE procedural_memory SET lesson = ? WHERE id = 'lesson'", (held,))
    assert (await proactive._surface_procedure(db, query)) is None


async def test_too_deep_json_is_held_and_does_not_stop_the_cache(db, cache_path):
    deep = "[" * 100_000 + "]" * 100_000  # json.loads raises RecursionError, not ValueError
    assert is_live_lesson(deep) is False
    await _row(db, "good", lesson=_lesson("owner"))
    await _row(db, "deep_lesson", lesson=_lesson("owner"))
    await _row(db, "deep_earned", tier="CORE")
    await db.execute(
        "UPDATE procedural_memory SET tool_trigger = ? WHERE id = 'deep_lesson'", (deep,)
    )
    await db.execute(
        "UPDATE procedural_memory SET context_tags = ? WHERE id = 'deep_earned'", (deep,)
    )
    assert await trigger_cache.regenerate(db) == 1
    assert set(_entries(cache_path)) == {"good"}


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("tool_trigger", "1"),
        ("tool_trigger", "true"),
        ("tool_trigger", '{"Bash": 1}'),
        ("tool_trigger", "[1, 2]"),
        ("steps", '"one step"'),
        ("context_tags", "[1]"),
        ("steps", "[" * 500 + "]" * 500),  # nested: would break the YAML writer
        ("steps", "[1]"),
        ("task_type", b"bytes"),  # binary: would break the JSON writer
        ("principle", b"bytes"),  # binary principle (an integer would be stored as text)
        ("trigger_spec", '{"patterns": ["a{999999999999999999999}"], "repeat": "every"}'),
    ],
)
async def test_a_field_the_advisor_cannot_read_leaves_only_that_row_out(
    db, cache_path, column, value
):
    await _row(db, "good", lesson=_lesson("owner"))
    await _row(db, "bad_lesson", lesson=_lesson("owner"))
    await _row(db, "bad_earned", tier="CORE")
    for pid in ("bad_lesson", "bad_earned"):
        await db.execute(f"UPDATE procedural_memory SET {column} = ? WHERE id = ?", (value, pid))
    await trigger_cache.regenerate(db)
    entries = _entries(cache_path)
    assert "good" in entries
    assert "bad_lesson" not in entries
    # An earned row's bad trigger_spec falls back to its tags (as before); every
    # other bad field leaves it out.
    assert ("bad_earned" in entries) is (column == "trigger_spec")


def test_an_overflowing_quantifier_is_refused_by_the_validator():
    from genesis.learning.procedural.lesson_shape import validate_trigger_spec

    with pytest.raises(ValueError):
        validate_trigger_spec({"patterns": ["a{999999999999999999999}"], "repeat": "every"})
