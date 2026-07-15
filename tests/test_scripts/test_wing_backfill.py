"""Tests for scripts/wing_backfill.py — the one-shot legacy wing backfill.

Covers the pure classification helpers (LLM response parsing with do-no-harm
validation, tag parsing, taxonomy stage) and the dual-store write path
(SQLite + Qdrant payload, including the revert-on-Qdrant-failure guarantee).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Load the script as a module (it's not a package — use importlib).
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "wing_backfill.py"
_spec = importlib.util.spec_from_file_location("wing_backfill", _SCRIPT_PATH)
wb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wb)

from genesis.memory.taxonomy import WINGS  # noqa: E402

# ── parse_llm_response — do-no-harm validation ──────────────────────────


def test_parse_valid_response():
    text = '{"a1b2c3d4": "memory", "e5f6a7b8": "dev_workflow"}'
    out = wb.parse_llm_response(text, {"a1b2c3d4", "e5f6a7b8"}, WINGS)
    assert out == {"a1b2c3d4": "memory", "e5f6a7b8": "dev_workflow"}


def test_parse_drops_invalid_wing():
    text = '{"a1b2c3d4": "not_a_wing", "e5f6a7b8": "routing"}'
    out = wb.parse_llm_response(text, {"a1b2c3d4", "e5f6a7b8"}, WINGS)
    assert out == {"e5f6a7b8": "routing"}


def test_parse_drops_general_verdict():
    """'general' means no change — never written back as a classification."""
    out = wb.parse_llm_response('{"a1b2c3d4": "general"}', {"a1b2c3d4"}, WINGS)
    assert out == {}


def test_parse_drops_unknown_ids():
    out = wb.parse_llm_response('{"zzzzzzzz": "memory"}', {"a1b2c3d4"}, WINGS)
    assert out == {}


def test_parse_survives_surrounding_prose():
    text = 'Here is the classification:\n{"a1b2c3d4": "channels"}\nDone.'
    out = wb.parse_llm_response(text, {"a1b2c3d4"}, WINGS)
    assert out == {"a1b2c3d4": "channels"}


def test_parse_garbage_returns_empty():
    for garbage in ("", "no json here", "[1, 2, 3]", '{"broken": ', '"just a string"'):
        assert wb.parse_llm_response(garbage, {"a1b2c3d4"}, WINGS) == {}


def test_parse_normalizes_case_and_whitespace():
    out = wb.parse_llm_response('{"a1b2c3d4": " Memory "}', {"a1b2c3d4"}, WINGS)
    assert out == {"a1b2c3d4": "memory"}


def test_parse_drops_non_string_values():
    out = wb.parse_llm_response(
        '{"a1b2c3d4": 42, "e5f6a7b8": "autonomy"}', {"a1b2c3d4", "e5f6a7b8"}, WINGS
    )
    assert out == {"e5f6a7b8": "autonomy"}


# ── prompt building ─────────────────────────────────────────────────────


def test_build_batch_prompt_contains_ids_and_wings():
    rows = [
        {"short_id": "a1b2c3d4", "content": "Investigated systemd unit deploy"},
        {"short_id": "e5f6a7b8", "content": "Checked CI\nstatus for PR"},
    ]
    prompt = wb.build_batch_prompt(rows, sorted(WINGS))
    assert "a1b2c3d4" in prompt and "e5f6a7b8" in prompt
    assert "dev_workflow" in prompt
    assert "\nstatus" not in prompt  # newlines flattened per snippet


def test_build_batch_prompt_truncates_content():
    rows = [{"short_id": "a1b2c3d4", "content": "x" * 5000}]
    prompt = wb.build_batch_prompt(rows, sorted(WINGS))
    line = [ln for ln in prompt.splitlines() if ln.startswith("a1b2c3d4")][0]
    assert len(line) < wb.CONTENT_SNIPPET_CHARS + 20


# ── tag parsing + room defaults ─────────────────────────────────────────


def test_parse_tags_json_list():
    assert wb.parse_tags('["infra", "deploy"]') == ["infra", "deploy"]


def test_parse_tags_comma_string_and_empty():
    assert wb.parse_tags("a, b") == ["a", "b"]
    assert wb.parse_tags(None) == []
    assert wb.parse_tags("") == []


def test_default_room_known_and_unknown_wing():
    from genesis.memory.taxonomy import ROOMS

    assert wb.default_room("memory") == ROOMS["memory"][0]
    assert wb.default_room("no_such_wing") == "uncategorized"


# ── stage 1: taxonomy classification ────────────────────────────────────


def test_stage1_accepts_confident_and_keeps_general():
    rows = [
        {  # keyword layer fires >= 0.6
            "memory_id": "m-keyword",
            "content": "Investigated CI failure in the GitHub Actions workflow",
            "tags": None,
        },
        {  # nothing fires — stays in remainder
            "memory_id": "m-vague",
            "content": "Checked something entirely unrecognizable xyzzy",
            "tags": None,
        },
    ]
    accepted, remainder = wb.stage1_taxonomy(rows)
    assert "m-keyword" in accepted
    wing, room = accepted["m-keyword"]
    assert wing in WINGS and wing != "general"
    assert [r["memory_id"] for r in remainder] == ["m-vague"]


# ── apply_updates: dual-store discipline ────────────────────────────────


@pytest.fixture
def mem_db(tmp_path):
    """Real aiosqlite DB with the memory_metadata shape the script touches."""
    import aiosqlite

    async def _make():
        db = await aiosqlite.connect(tmp_path / "test.db")
        await db.execute(
            """CREATE TABLE memory_metadata (
                memory_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL DEFAULT '2026-01-01',
                collection TEXT NOT NULL DEFAULT 'episodic_memory',
                embedding_status TEXT NOT NULL DEFAULT 'embedded',
                wing TEXT, room TEXT,
                deprecated INTEGER NOT NULL DEFAULT 0
            )"""
        )
        await db.executemany(
            "INSERT INTO memory_metadata (memory_id, wing, room, embedding_status)"
            " VALUES (?, ?, ?, ?)",
            [
                ("m-embedded", "general", "uncategorized", "embedded"),
                ("m-ftsonly", None, None, "fts5_only"),
            ],
        )
        await db.commit()
        return db

    return _make


def _row(memory_id: str, embedding_status: str) -> dict:
    return {
        "memory_id": memory_id,
        "collection": "episodic_memory",
        "embedding_status": embedding_status,
        "content": "",
        "tags": None,
        "short_id": memory_id[:8],
    }


async def test_apply_updates_writes_both_stores(mem_db):
    db = await mem_db()
    qdrant = MagicMock()
    calls = []
    with patch(
        "genesis.qdrant.collections.update_payload",
        side_effect=lambda *a, **k: calls.append(k),
    ):
        applied, failed = await wb.apply_updates(
            db,
            qdrant,
            {"m-embedded": _row("m-embedded", "embedded")},
            {"m-embedded": ("infrastructure", "deploy")},
        )
    assert (applied, failed) == (1, 0)
    cur = await db.execute("SELECT wing, room FROM memory_metadata WHERE memory_id = 'm-embedded'")
    assert await cur.fetchone() == ("infrastructure", "deploy")
    assert calls and calls[0]["payload"]["wing"] == "infrastructure"
    assert calls[0]["payload"]["life_domain"] == "genesis"
    await db.close()


async def test_apply_updates_skips_qdrant_for_fts5_only(mem_db):
    db = await mem_db()
    with patch("genesis.qdrant.collections.update_payload") as up:
        applied, failed = await wb.apply_updates(
            db,
            MagicMock(),
            {"m-ftsonly": _row("m-ftsonly", "fts5_only")},
            {"m-ftsonly": ("memory", "retrieval")},
        )
    assert (applied, failed) == (1, 0)
    up.assert_not_called()
    cur = await db.execute("SELECT wing FROM memory_metadata WHERE memory_id = 'm-ftsonly'")
    assert await cur.fetchone() == ("memory",)
    await db.close()


async def test_apply_updates_reverts_sqlite_on_qdrant_failure(mem_db):
    """Cross-store mirror discipline: a Qdrant failure must not leave SQLite
    claiming the new wing."""
    db = await mem_db()
    with patch(
        "genesis.qdrant.collections.update_payload",
        side_effect=RuntimeError("qdrant down"),
    ):
        applied, failed = await wb.apply_updates(
            db,
            MagicMock(),
            {"m-embedded": _row("m-embedded", "embedded")},
            {"m-embedded": ("channels", "telegram")},
        )
    assert (applied, failed) == (0, 1)
    cur = await db.execute("SELECT wing, room FROM memory_metadata WHERE memory_id = 'm-embedded'")
    assert await cur.fetchone() == ("general", "uncategorized")
    await db.close()


async def test_apply_updates_skips_row_reclassified_meanwhile(mem_db):
    """Idempotence: a row that gained a real wing since the read is not
    overwritten (the UPDATE's WHERE re-checks the backlog condition)."""
    db = await mem_db()
    await db.execute(
        "UPDATE memory_metadata SET wing = 'routing', room = 'providers'"
        " WHERE memory_id = 'm-embedded'"
    )
    await db.commit()
    with patch("genesis.qdrant.collections.update_payload"):
        await wb.apply_updates(
            db,
            MagicMock(),
            {"m-embedded": _row("m-embedded", "embedded")},
            {"m-embedded": ("channels", "telegram")},
        )
    cur = await db.execute("SELECT wing FROM memory_metadata WHERE memory_id = 'm-embedded'")
    assert await cur.fetchone() == ("routing",)
    await db.close()


# ── stage 2: LLM batch flow with a fake router ──────────────────────────


class _FakeResult:
    def __init__(self, content: str):
        self.content = content


class _FakeRouter:
    """Implements the real Router.route_call contract used by the script."""

    def __init__(self, responses: list[str]):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    async def route_call(self, call_site_id, messages, **kwargs):
        self.calls.append((call_site_id, messages))
        return _FakeResult(self._responses.pop(0))


async def test_stage2_llm_batches_and_maps_short_ids():
    rows = [
        {
            "memory_id": "aaaaaaaa-1111",
            "short_id": "aaaaaaaa",
            "content": "systemd deploy",
            "tags": None,
        },
        {
            "memory_id": "bbbbbbbb-2222",
            "short_id": "bbbbbbbb",
            "content": "unknown blob",
            "tags": None,
        },
    ]
    router = _FakeRouter([json.dumps({"aaaaaaaa": "infrastructure", "bbbbbbbb": "general"})])
    out = await wb.stage2_llm(router, rows)
    assert out == {"aaaaaaaa-1111": ("infrastructure", wb.default_room("infrastructure"))}
    assert router.calls[0][0] == "wing_backfill"


async def test_stage2_llm_failure_leaves_rows_unclassified():
    class _Boom:
        async def route_call(self, *a, **k):
            raise RuntimeError("all providers down")

    rows = [{"memory_id": "aaaaaaaa-1111", "short_id": "aaaaaaaa", "content": "x", "tags": None}]
    out = await wb.stage2_llm(_Boom(), rows)
    assert out == {}
