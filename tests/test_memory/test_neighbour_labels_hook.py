"""Labelled links in the proactive hook's breadcrumbs (memory spec MW-11).

``_breadcrumbs`` keeps its shape (``related_ids``: 8-char prefixes, pinned by
``test_recall_read_pool.py``). A separate step adds ``related_labels`` with the
shared exclusions, never for a hidden memory, and its failure costs nothing but
the labels. The hint ids shown are recorded on the recall trace, so a later
expand in the same session can be counted as a followed link.
"""

from __future__ import annotations

from types import SimpleNamespace

import aiosqlite
import pytest

from genesis.memory import proactive as P
from genesis.memory.recall_trace import build_record

LIVE = "aaaaaaaa-0000-0000-0000-000000000001"
HIDDEN = "bbbbbbbb-0000-0000-0000-000000000002"
CRED = "cccccccc-0000-0000-0000-000000000003"
SEED = "dddddddd-0000-0000-0000-000000000004"
FAKE_SECRET = "fakeHookLabelSecret-Mt6r"


def test_render_prints_labels_beside_hint_ids():
    lines = P.render_cc_hook(
        [
            {
                "memory_id": SEED,
                "content": "a delivered memory",
                "related_ids": ["aaaaaaaa", "cccccccc"],
                "related_labels": {"aaaaaaaa": "Deploy guard refuses a dirty tree…"},
            }
        ]
    )
    assert "| → id:aaaaaaaa (Deploy guard refuses a dirty tree…), id:cccccccc]" in lines[0]


def test_render_without_labels_is_unchanged():
    lines = P.render_cc_hook(
        [{"memory_id": SEED, "content": "a delivered memory", "related_ids": ["aaaaaaaa"]}]
    )
    assert lines[0].endswith("| → id:aaaaaaaa] a delivered memory")


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(
        "CREATE TABLE memory_metadata (memory_id TEXT PRIMARY KEY, created_at TEXT, wing TEXT, "
        "invalid_at TEXT, deprecated INTEGER, origin_class TEXT);"
        "CREATE TABLE memory_links (source_id TEXT, target_id TEXT, strength REAL);"
        "CREATE TABLE knowledge_units (id TEXT, qdrant_id TEXT, project_type TEXT, domain TEXT);"
    )
    await conn.executemany(
        "INSERT INTO memory_metadata VALUES (?, NULL, NULL, NULL, ?, 'first_party')",
        [(LIVE, 0), (HIDDEN, 1), (CRED, 0), (SEED, 0)],
    )
    await conn.executemany(
        "INSERT INTO memory_links VALUES (?, ?, ?)",
        [(SEED, LIVE, 0.9), (SEED, HIDDEN, 0.8)],
    )
    try:
        yield conn
    finally:
        await conn.close()


def _qdrant(contents: dict[str, str]):
    def retrieve(collection_name, ids, with_payload):
        return [
            SimpleNamespace(id=i, payload={"content": contents[i]}) for i in ids if i in contents
        ]

    return SimpleNamespace(retrieve=retrieve)


async def test_breadcrumbs_keep_prefixes_and_remember_full_ids(db):
    dicts = [{"memory_id": SEED}]
    await P._breadcrumbs(db, dicts)
    assert dicts[0]["related_ids"] == ["aaaaaaaa", "bbbbbbbb"]
    assert dicts[0]["_related_full"] == [LIVE, HIDDEN]


async def test_breadcrumb_labels_never_label_a_hidden_or_credential_id(db):
    await db.execute("INSERT INTO memory_links VALUES (?, ?, 0.85)", (SEED, CRED))
    await db.execute(
        "INSERT INTO knowledge_units VALUES ('u', ?, 'reference', 'reference.credentials')", (CRED,)
    )
    dicts = [{"memory_id": SEED}]
    await P._breadcrumbs(db, dicts)
    q = _qdrant(
        {
            LIVE: "Hook budget is anchored to process start time",
            HIDDEN: "a superseded rule",
            CRED: f"Router login Value: {FAKE_SECRET}",
        }
    )
    await P._breadcrumb_labels(db, dicts, q)
    # Top 2 by strength: LIVE (0.9) and CRED (0.85); HIDDEN (0.8) is the third.
    assert dicts[0]["related_ids"] == ["aaaaaaaa", "cccccccc"]
    assert dicts[0]["related_labels"] == {"aaaaaaaa": "Hook budget is anchored to process…"}
    assert FAKE_SECRET not in repr(dicts)


async def test_breadcrumb_label_failure_keeps_the_hint_ids(db):
    dicts = [{"memory_id": SEED}]
    await P._breadcrumbs(db, dicts)

    def boom(**_kw):
        raise RuntimeError("qdrant down")

    await P._breadcrumb_labels(db, dicts, SimpleNamespace(retrieve=boom))
    assert dicts[0]["related_ids"] == ["aaaaaaaa", "bbbbbbbb"]
    assert "related_labels" not in dicts[0]


async def test_breadcrumb_labels_with_no_hints_read_nothing(db):
    calls = []
    dicts = [{"memory_id": "no-links"}]
    await P._breadcrumb_labels(db, dicts, SimpleNamespace(retrieve=lambda **kw: calls.append(kw)))
    assert calls == []
    assert "related_labels" not in dicts[0]


def test_shown_hints_are_the_full_ids_in_order():
    dicts = [
        {"memory_id": "x", "_related_full": [LIVE, HIDDEN]},
        {"memory_id": "y"},
        {"memory_id": "z", "_related_full": [LIVE, CRED]},
    ]
    assert P._shown_hints(dicts) == [LIVE, HIDDEN, CRED]


def test_trace_record_persists_the_shown_hints():
    assert build_record({"hints": [LIVE, HIDDEN]})["hints"] == [LIVE, HIDDEN]
    assert build_record({})["hints"] == []


async def test_breadcrumb_labels_skip_a_hidden_hint(db):
    dicts = [{"memory_id": SEED}]
    await P._breadcrumbs(db, dicts)
    assert dicts[0]["related_ids"] == ["aaaaaaaa", "bbbbbbbb"]  # the id itself stays (#5051)
    await P._breadcrumb_labels(db, dicts, _qdrant({LIVE: "live note", HIDDEN: "a superseded rule"}))
    assert dicts[0]["related_labels"] == {"aaaaaaaa": "live note"}
