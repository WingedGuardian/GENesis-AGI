"""Authoring a lesson through procedure_store (advisory-lessons T-4)."""

from __future__ import annotations

import contextlib
import json
from unittest.mock import MagicMock

import aiosqlite
import pytest

from genesis.db.crud import procedural
from genesis.learning.procedural import matcher, trigger_cache
from genesis.learning.procedural.embedding import pack_embedding
from genesis.learning.procedural.operations import store_procedure_checked
from genesis.mcp.memory_mcp import mcp

_VEC = pack_embedding([1.0] + [0.0] * 1023)


@pytest.fixture
async def env(tmp_path, monkeypatch):
    """A real in-memory DB behind the MCP memory server, a redirected cache, a fixed embedding."""
    import genesis.mcp.memory.procedural as procedural_tools
    import genesis.mcp.memory_mcp as mod
    from genesis.db.schema import create_all_tables

    cache = tmp_path / "procedure_triggers.yaml"
    monkeypatch.setattr(trigger_cache, "_CACHE_PATH", cache)
    monkeypatch.delenv("GENESIS_SESSION_ORIGIN", raising=False)

    async def fixed_embedding(_principle):
        return _VEC

    monkeypatch.setattr(procedural_tools, "_embed_principle_for_hook", fixed_embedding)
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)
        old = mod._store, mod._db, mod._retriever
        mod._store, mod._db, mod._retriever = MagicMock(), db, MagicMock()
        try:
            store = (await mcp.get_tools())["procedure_store"].fn
            yield db, store, cache
        finally:
            mod._store, mod._db, mod._retriever = old


_ARGS = {
    "task_type": "deploy",
    "principle": "launch the deploy detached",
    "steps": ["use setsid"],
    "tools_used": ["Bash"],
    "context_tags": ["deploy"],
    "tool_trigger": ["Bash"],
}


def _cached(cache) -> dict:
    return {
        t["procedure_id"]: t for t in json.loads(cache.with_suffix(".json").read_text())["triggers"]
    }


async def _only_row(db) -> dict:
    rows = await db.execute_fetchall("SELECT * FROM procedural_memory")
    assert len(rows) == 1
    return dict(rows[0])


async def test_a_foreground_lesson_is_live_and_cached_unreviewed(env):
    db, store, cache = env
    response = await store(**_ARGS, trigger_patterns=[r"update\.sh"], repeat="session")
    row = await _only_row(db)
    lesson = json.loads(row["lesson"])
    assert (
        lesson["origin"],
        lesson["authored_in"],
        lesson["authored_via"],
        lesson["reviewed"],
    ) == (
        "first_party",
        "foreground",
        "mcp",
        False,
    )
    assert json.loads(row["trigger_spec"]) == {"patterns": [r"update\.sh"], "repeat": "session"}
    assert row["activation_tier"] == "ADVISORY"
    assert "live" in response
    assert _cached(cache)[row["id"]]["label"] == "unreviewed"


async def test_an_external_lesson_is_held_and_not_cached(env, monkeypatch):
    db, store, cache = env
    monkeypatch.setenv("GENESIS_SESSION_ORIGIN", "external_untrusted")
    response = await store(**_ARGS, trigger_patterns=[r"update\.sh"])
    row = await _only_row(db)
    assert row["activation_tier"] == "LIBRARY"
    assert "HELD" in response
    await trigger_cache.regenerate(db)  # the held write itself leaves the cache alone
    assert row["id"] not in _cached(cache)
    # Written out of every reader's sight until reviewed (owner ruling).
    from genesis.learning.procedural.lesson_shape import HELD_REASON

    assert (row["deprecated"], row["deprecated_reason"]) == (1, HELD_REASON)
    assert await procedural.list_active(db) == []
    # Held from recall too (owner ruling): neither matcher path returns it.
    assert await matcher.find_best_match(db, "deploy", ["deploy"]) is None
    assert await matcher.find_relevant(db, ["deploy"], min_confidence=0.0) == []


async def test_an_external_refine_of_a_live_lesson_makes_a_held_sibling(env, monkeypatch):
    db, store, _ = env
    await store(**_ARGS, trigger_patterns=[r"update\.sh"])
    original = await _only_row(db)
    monkeypatch.setenv("GENESIS_SESSION_ORIGIN", "external_untrusted")
    await store(**{**_ARGS, "principle": "launch the deploy detached"}, trigger_patterns=["rm -rf"])
    rows = {r["id"]: dict(r) for r in await db.execute_fetchall("SELECT * FROM procedural_memory")}
    assert len(rows) == 2
    assert rows[original["id"]]["trigger_spec"] == original["trigger_spec"]  # untouched
    sibling = next(r for pid, r in rows.items() if pid != original["id"])
    assert json.loads(sibling["lesson"])["origin"] == "external_untrusted"
    assert sibling["activation_tier"] == "LIBRARY"
    assert sibling["deprecated"] == 1  # held: out of every reader's sight


async def test_a_trusted_refine_with_lesson_parameters_rewrites_the_spec(env):
    db, store, _ = env
    await store(**_ARGS, trigger_patterns=[r"update\.sh"])
    await store(**_ARGS, trigger_patterns=[r"deploy_code_only\.sh"], repeat="every")
    row = await _only_row(db)
    assert json.loads(row["trigger_spec"]) == {
        "patterns": [r"deploy_code_only\.sh"],
        "repeat": "every",
    }


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"trigger_patterns": ["("]}, "compile"),
        ({"trigger_patterns": ["x"], "repeat": "sometimes"}, "repeat"),
        ({"trigger_patterns": ["x"], "tool_trigger": ["not a tool"]}, "tool"),
        ({"trigger_patterns": ["x"], "tool_trigger": ["ssh", "scp", "pytest", "ruff", "gitleaks", "pgrep", "incus", "gh", "git push"]}, "8"),
    ],
)
async def test_an_invalid_lesson_is_refused_and_nothing_is_stored(env, override, reason):
    db, store, _ = env
    response = await store(**{**_ARGS, **override})
    assert response.startswith("Refused:")
    assert reason in response
    assert await db.execute_fetchall("SELECT id FROM procedural_memory") == []


async def test_a_call_without_patterns_is_unchanged(env):
    db, store, cache = env
    response = await store(**_ARGS)
    row = await _only_row(db)
    assert (row["lesson"], row["trigger_spec"], row["activation_tier"]) == (None, None, "LIBRARY")
    assert "Lesson" not in response
    assert not cache.with_suffix(".json").exists()  # no lesson, no rebuild


async def test_a_refine_that_changes_an_earned_trigger_drops_its_stale_spec(env):
    db, _, _ = env
    first = await store_procedure_checked(
        db, principle_embedding=_VEC, **{**_ARGS, "tool_trigger": ["ssh"]}
    )
    await procedural.update(
        db, first.procedure_id, trigger_spec={"patterns": ["ssh"], "repeat": "every"}
    )
    await store_procedure_checked(
        db, principle_embedding=_VEC, **{**_ARGS, "tool_trigger": ["Read"]}
    )
    row = await _only_row(db)
    assert json.loads(row["tool_trigger"]) == ["Read"]
    assert row["trigger_spec"] is None


async def test_a_lesson_call_never_rewrites_an_earned_row(env, monkeypatch):
    db, store, _ = env
    earned = await store_procedure_checked(
        db, principle_embedding=_VEC, activation_tier="CORE", **_ARGS
    )
    for origin in (None, "external_untrusted"):
        if origin:
            monkeypatch.setenv("GENESIS_SESSION_ORIGIN", origin)
        await store(**_ARGS, trigger_patterns=[r"update\.sh"])
    row = await procedural.get_by_id(db, earned.procedure_id)
    assert (row["lesson"], row["activation_tier"]) == (None, "CORE")
    assert len(await db.execute_fetchall("SELECT id FROM procedural_memory")) == 3


async def test_a_plain_teach_never_lands_in_a_lesson(env):
    db, store, _ = env
    await store(**_ARGS, trigger_patterns=[r"update\.sh"])
    await store(**{**_ARGS, "tool_trigger": ["Read"]})
    rows = await db.execute_fetchall(
        "SELECT lesson, tool_trigger, trigger_spec FROM procedural_memory"
    )
    assert len(rows) == 2
    lesson_row = next(r for r in rows if r["lesson"] is not None)
    assert json.loads(lesson_row["tool_trigger"]) == ["Bash"]  # untouched


async def test_an_unchanged_trigger_keeps_its_spec(env):
    db, _, _ = env
    first = await store_procedure_checked(
        db, principle_embedding=_VEC, **{**_ARGS, "tool_trigger": ["Edit"]}
    )
    spec = {"patterns": ["x"], "repeat": "every"}
    await procedural.update(db, first.procedure_id, trigger_spec=spec)
    await store_procedure_checked(
        db, principle_embedding=_VEC, **{**_ARGS, "tool_trigger": ["Edit"]}
    )
    row = await _only_row(db)
    assert json.loads(row["trigger_spec"]) == spec


async def test_a_failed_cache_rebuild_is_reported(env, monkeypatch):
    _, store, _ = env

    async def broken(_db):
        raise OSError("disk full")

    monkeypatch.setattr(trigger_cache, "regenerate", broken)
    response = await store(**_ARGS, trigger_patterns=[r"update\.sh"])
    assert "rebuild failed" in response


async def test_the_pattern_cap_refusal_names_the_derived_patterns(env):
    db, store, _ = env
    response = await store(**{**_ARGS, "tool_trigger": ["ssh", "scp", "pytest", "ruff", "gitleaks", "pgrep", "incus", "gh", "git push"]}, trigger_patterns=["x"])
    assert response.startswith("Refused:")
    assert "derived from tool_trigger" in response


async def test_held_lessons_never_crowd_live_rows_out_of_recall(env):
    db, _, _ = env
    held = json.dumps({
        "authored_via": "mcp", "authored_in": "dispatched", "origin": "external_untrusted",
        "reviewed": False, "authored_at": "2026-10-14T00:00:00Z",
    })
    common = {"principle": "p", "steps": ["s"], "tools_used": ["Bash"], "context_tags": ["deploy"],
              "created_at": "2026-10-14T00:00:00Z"}
    for i in range(201):  # past both limits: 50 by task type, 200 across types
        await procedural.create(db, id=f"held-{i}", task_type="deploy", confidence=0.99, **common)
        await db.execute("UPDATE procedural_memory SET lesson = ? WHERE id = ?", (held, f"held-{i}"))
    await procedural.create(db, id="live", task_type="deploy", confidence=0.5, **common)
    await db.commit()
    assert (await matcher.find_best_match(db, "deploy", ["deploy"])).procedure_id == "live"
    assert [m.procedure_id for m in await matcher.find_relevant(db, ["deploy"], min_confidence=0.0)] == ["live"]


async def test_a_regenerate_waits_for_the_cache_lock(env):
    import asyncio
    import fcntl

    db, _, cache = env
    with open(cache.with_suffix(".lock"), "a") as other_writer:
        fcntl.flock(other_writer.fileno(), fcntl.LOCK_EX)
        task = asyncio.create_task(trigger_cache.regenerate(db))
        await asyncio.sleep(0.2)
        assert not task.done()  # held off while another writer holds the lock
        fcntl.flock(other_writer.fileno(), fcntl.LOCK_UN)
        await asyncio.wait_for(task, 5)
    # Two writers in one event loop finish without deadlocking the loop.
    await asyncio.wait_for(asyncio.gather(trigger_cache.regenerate(db), trigger_cache.regenerate(db)), 5)
    assert cache.with_suffix(".json").exists()


async def test_lessons_never_crowd_an_earned_row_out_of_a_refine(env):
    db, store, _ = env
    await store(**_ARGS)  # the earned row a later plain teach should refine
    (earned,) = [dict(r) for r in await db.execute_fetchall("SELECT * FROM procedural_memory")]
    lesson = json.dumps({
        "authored_via": "mcp", "authored_in": "foreground", "origin": "first_party",
        "reviewed": False, "authored_at": "2026-10-14T00:00:00Z",
    })
    for i in range(51):  # past list_by_task_type's limit of 50
        await procedural.create(
            db, id=f"lesson-{i}", task_type="deploy", principle="p", steps=["s"], tools_used=["Bash"],
            context_tags=["deploy"], created_at="2026-10-14T00:00:00Z", confidence=0.99,
        )
        await db.execute("UPDATE procedural_memory SET lesson = ? WHERE id = ?", (lesson, f"lesson-{i}"))
    await db.commit()
    await store(**_ARGS)
    rows = await db.execute_fetchall("SELECT id FROM procedural_memory WHERE lesson IS NULL")
    assert [r["id"] for r in rows] == [earned["id"]]  # refined in place, no duplicate


_HELD = {
    "authored_via": "mcp", "authored_in": "dispatched", "origin": "external_untrusted",
    "reviewed": False, "authored_at": "2026-10-14T00:00:00Z",
}


async def _lesson_row(db, pid, *, lesson, task_type="deploy", confidence=0.99, embedding=None, **extra):
    await procedural.create(
        db, id=pid, task_type=task_type, principle=f"principle {pid}", steps=["s"],
        tools_used=["Bash"], context_tags=["deploy"], created_at="2026-10-14T00:00:00Z",
        confidence=confidence, principle_embedding=embedding, **extra,
    )
    await db.execute("UPDATE procedural_memory SET lesson = ? WHERE id = ?", (json.dumps(lesson), pid))
    await db.commit()


async def test_a_lesson_refine_finds_its_row_past_fifty_others(env):
    db, store, _ = env
    await store(**_ARGS, trigger_patterns=[r"update\.sh"])
    (mine,) = [dict(r) for r in await db.execute_fetchall("SELECT id FROM procedural_memory")]
    other = pack_embedding([0.0, 1.0] + [0.0] * 1022)  # a different principle
    live = {**_HELD, "origin": "first_party", "authored_in": "foreground"}
    for i in range(51):  # all ranked above the lesson being re-authored
        await _lesson_row(db, f"other-{i}", lesson=live, embedding=other)
    await store(**_ARGS, trigger_patterns=[r"deploy_code_only\.sh"])
    row = dict((await db.execute_fetchall(
        "SELECT trigger_spec FROM procedural_memory WHERE id = ?", (mine["id"],)))[0])
    assert json.loads(row["trigger_spec"])["patterns"] == [r"deploy_code_only\.sh"]  # refined
    assert len(await db.execute_fetchall("SELECT id FROM procedural_memory")) == 52  # no duplicate


async def test_an_unreadable_stored_trigger_is_replaced(env):
    db, store, _ = env
    await store(**_ARGS)
    (row,) = [dict(r) for r in await db.execute_fetchall("SELECT id FROM procedural_memory")]
    await db.execute("UPDATE procedural_memory SET tool_trigger = '{bad' WHERE id = ?", (row["id"],))
    await db.commit()
    response = await store(**_ARGS)
    assert not response.startswith("Refused")
    (after,) = [dict(r) for r in await db.execute_fetchall("SELECT tool_trigger FROM procedural_memory")]
    assert json.loads(after["tool_trigger"]) == ["Bash"]


async def test_held_lessons_never_reach_novelty_or_validation_checks(env, monkeypatch):
    from genesis.learning.procedural import extractor, validation_gate

    db, _, _ = env
    await _lesson_row(db, "held", lesson=_HELD, success_count=5)
    assert await procedural.find_by_context_overlap(db, ["deploy"]) == []
    flags: list[str] = []
    assert not await validation_gate._check_regression(db, "deploy", "principle held", flags)
    seen = []

    async def record(_db, *args, **kwargs):
        seen.append(kwargs.get("live_only"))
        return []

    monkeypatch.setattr(procedural, "list_by_task_type", record)
    monkeypatch.setattr(procedural, "list_active", record)
    for call in (
        extractor._principle_is_novel(
            db, task_type="deploy", new_principle="a principle", embedder=MagicMock()
        ),
        extractor._cross_type_duplicate(
            db, task_type="deploy", principle="a principle", new_steps=None,
            new_emb=[1.0] + [0.0] * 1023, router=MagicMock(),
        ),
    ):
        with contextlib.suppress(Exception):  # only the listing arguments matter here
            await call
    assert seen == [True, True]


async def test_lessons_never_crowd_earned_rows_out_of_the_promoter_or_the_cache(env):
    from genesis.learning.procedural import promoter

    db, store, cache = env
    await procedural.create(
        db, id="earned", task_type="deploy", principle="p", steps=["s"], tools_used=["Bash"],
        context_tags=["deploy"], created_at="2026-10-14T00:00:00Z", confidence=0.6,
        activation_tier="ADVISORY", tool_trigger=["Bash"], success_count=9, draft=0,
    )
    live = {**_HELD, "origin": "first_party", "authored_in": "foreground"}
    for i in range(101):  # past both windows of 100, all ranked above the earned row
        await _lesson_row(db, f"l-{i}", lesson=live, activation_tier="ADVISORY",
                          tool_trigger=["Bash"])
        await db.execute(
            "UPDATE procedural_memory SET trigger_spec = ? WHERE id = ?",
            (json.dumps({"patterns": ["x"], "repeat": "every"}), f"l-{i}"),
        )
    await db.commit()
    await trigger_cache.regenerate(db)
    assert "earned" in _cached(cache)
    await db.execute("UPDATE procedural_memory SET confidence = 0.1, failure_count = 9 WHERE id = 'earned'")
    await db.commit()
    await promoter.promote_and_demote(db)
    assert (await procedural.get_by_id(db, "earned"))["quarantined"] == 1  # it was evaluated


async def test_a_held_write_leaves_the_cache_alone(env, monkeypatch):
    from genesis.learning.procedural import operations

    db, store, _ = env
    calls = []

    async def counted(db_):
        calls.append(1)
        return True

    monkeypatch.setattr(operations, "_regenerate_cache", counted)
    monkeypatch.setenv("GENESIS_SESSION_ORIGIN", "external_untrusted")
    await store(**_ARGS, trigger_patterns=[r"update\.sh"])
    assert calls == []
    monkeypatch.delenv("GENESIS_SESSION_ORIGIN")
    await store(**{**_ARGS, "task_type": "other"}, trigger_patterns=[r"update\.sh"])
    assert calls == [1]  # a live lesson still rebuilds it


async def test_a_held_record_never_updates_a_live_row_whatever_the_caller_says(env):
    db, store, _ = env
    await store(**_ARGS, trigger_patterns=[r"update\\.sh"])
    (live,) = [dict(r) for r in await db.execute_fetchall("SELECT * FROM procedural_memory")]
    held = {**_HELD}
    result = await store_procedure_checked(
        db, task_type="deploy", principle=live["principle"], steps=["s"], tools_used=["Bash"],
        context_tags=["deploy"], tool_trigger=["Bash"], principle_embedding=_VEC,
        trigger_spec={"patterns": ["x"], "repeat": "every"}, lesson=held,
    )  # no caller_origin passed
    assert result.action == "created"
    row = dict((await db.execute_fetchall(
        "SELECT lesson, deprecated FROM procedural_memory WHERE id = ?", (live["id"],)))[0])
    assert json.loads(row["lesson"])["origin"] != "external_untrusted" and row["deprecated"] == 0


async def test_a_command_word_becomes_a_command_spec_on_every_write(env):
    db, store, cache = env
    await store(**{**_ARGS, "tool_trigger": ["ssh"]})  # a plain teach: an earned row
    row = await _only_row(db)
    assert json.loads(row["trigger_spec"]) == {"patterns": [], "commands": ["ssh"], "repeat": "every"}
    await store(**{**_ARGS, "tool_trigger": ["git push"]})  # a refine naming a new command
    row = await _only_row(db)
    assert json.loads(row["trigger_spec"])["commands"] == ["git push"]


async def test_a_lesson_merges_derived_commands_with_its_own(env):
    db, store, cache = env
    await store(**{**_ARGS, "tool_trigger": ["ssh"]}, trigger_patterns=[r"update\.sh"])
    spec = json.loads((await _only_row(db))["trigger_spec"])
    assert spec["patterns"] == [r"update\.sh"] and spec["commands"] == ["ssh"]


async def test_commands_narrow_only_a_bash_only_row_and_bash_widens_it_again(env):
    db, store, _ = env
    result = await store_procedure_checked(
        db, principle_embedding=_VEC, **{**_ARGS, "tool_trigger": ["ssh", "Edit"]}
    )
    row = await _only_row(db)
    assert row["trigger_spec"] is None  # Edit keeps firing on its tags
    assert any("not stored" in w for w in result.warnings)
    await store_procedure_checked(db, principle_embedding=_VEC, **{**_ARGS, "tool_trigger": ["ssh"]})
    assert json.loads((await _only_row(db))["trigger_spec"])["commands"] == ["ssh"]
    await store_procedure_checked(db, principle_embedding=_VEC, **{**_ARGS, "tool_trigger": ["Bash"]})
    assert (await _only_row(db))["trigger_spec"] is None  # every Bash call again


async def test_an_extracted_row_gets_its_commands_spec():
    from genesis.db.schema import create_all_tables
    from genesis.learning.procedural.operations import store_procedure

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await create_all_tables(db)
        pid = await store_procedure(
            db, task_type="t", principle="p", steps=["s"], tools_used=[], context_tags=["x"],
            tool_trigger=["git push"],
        )
        row = await procedural.get_by_id(db, pid)
        assert json.loads(row["trigger_spec"]) == {"patterns": [], "commands": ["git push"], "repeat": "every"}
