"""Authoring a lesson through procedure_store (advisory-lessons T-4)."""

from __future__ import annotations

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
    assert row["id"] not in _cached(cache)
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
        ({"trigger_patterns": [f"p{i}" for i in range(8)], "tool_trigger": ["ssh"]}, "8"),
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
        db, principle_embedding=_VEC, **{**_ARGS, "tool_trigger": ["ssh"]}
    )
    spec = {"patterns": ["ssh"], "repeat": "every"}
    await procedural.update(db, first.procedure_id, trigger_spec=spec)
    await store_procedure_checked(
        db, principle_embedding=_VEC, **{**_ARGS, "tool_trigger": ["ssh"]}
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
    response = await store(
        **{**_ARGS, "tool_trigger": ["ssh"]}, trigger_patterns=[f"p{i}" for i in range(8)]
    )
    assert response.startswith("Refused:")
    assert "derived from shell commands" in response
