"""The knowledge browser must not be a second door onto the reference store.

Both stores live in ``knowledge_units``. The reference store is the
``project_type='reference'`` partition, and a reference row carries its stored
value in ``body`` — 51 of the 101 rows on the install where this was found were
``reference.credentials``, 21 were ``reference.network``.

``references.py`` renders that partition behind value-free summaries, kind
masking and an explicit reveal step, and it already scopes itself to it:
``references_detail`` refuses any row whose ``project_type`` is not
``reference``. The partition was ONE-SIDED. Nothing in ``knowledge.py``
refused the reference rows, so ``SELECT *`` handed back every column including
``body``.

These routes carry no auth predicate, and none would have helped: the
app-level mutation gate exempts GET and the blueprint gate exempts ``/api/``,
so every GET here answers an anonymous caller on EVERY install — a configured
one included. The partition, not authentication, is what closes this.
"""

from __future__ import annotations

import pytest

pytest.importorskip("aiosqlite")

import aiosqlite  # noqa: E402

from genesis.db.crud import knowledge as knowledge_crud  # noqa: E402
from genesis.memory.reference_ops import REFERENCE_PROJECT  # noqa: E402

_SENTINEL = "sentinel-value-must-never-be-served-0123456789"


async def _seed(db: aiosqlite.Connection) -> None:
    """A row per partition, plus the NULL case the exclusion must not eat."""
    await db.execute("""
        CREATE TABLE knowledge_units (
            id TEXT PRIMARY KEY, project_type TEXT, domain TEXT, concept TEXT,
            body TEXT, tags TEXT, source_pipeline TEXT, ingested_at TEXT,
            confidence REAL, origin_class TEXT, qdrant_id TEXT
        )
    """)
    await db.execute("""
        CREATE VIRTUAL TABLE knowledge_fts USING fts5(
            unit_id, concept, body, tags, domain, project_type
        )
    """)
    rows = [
        (
            "ref-1",
            REFERENCE_PROJECT,
            "reference.credentials",
            "api key",
            f"Value: {_SENTINEL}",
            "",
            "curated",
            "2026-09-01",
            1.0,
            "c",
            "q1",
        ),
        (
            "kb-1",
            "genesis",
            "genesis.arch",
            "architecture note",
            "an ordinary knowledge body",
            "",
            "curated",
            "2026-09-02",
            1.0,
            "c",
            "q2",
        ),
        # project_type NULL — the case a bare `!= 'reference'` silently drops,
        # because that comparison is NULL and therefore not TRUE.
        (
            "kb-null",
            None,
            "misc",
            "untyped note",
            "an untyped knowledge body",
            "",
            "curated",
            "2026-09-03",
            1.0,
            "c",
            "q3",
        ),
    ]
    for r in rows:
        await db.execute("INSERT INTO knowledge_units VALUES (?,?,?,?,?,?,?,?,?,?,?)", r)
        await db.execute(
            "INSERT INTO knowledge_fts (unit_id, concept, body, tags, domain, project_type)"
            " VALUES (?,?,?,?,?,?)",
            (r[0], r[3], r[4], r[5], r[2], r[1]),
        )
    await db.commit()


@pytest.fixture()
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        await _seed(conn)
        yield conn


@pytest.mark.asyncio
async def test_search_excludes_the_reference_partition(db):
    """The value must not come back, and the marker is COUNTED, never printed.

    pytest rewrites assertions and prints the compared objects, so asserting on
    the rows themselves would emit the very value this test exists to keep out
    of reach — into CI logs, at the moment it fires.
    """
    rows = await knowledge_crud.search_fts(db, "Value", exclude_project=REFERENCE_PROJECT, limit=50)
    leaked = len([r for r in rows if _SENTINEL in (r.get("body") or "")])
    assert leaked == 0, f"{leaked} reference row(s) served by the knowledge search"


@pytest.mark.asyncio
async def test_search_still_returns_ordinary_knowledge(db):
    """Guard-the-guard: an exclusion that returned nothing would pass the above."""
    rows = await knowledge_crud.search_fts(
        db, "knowledge", exclude_project=REFERENCE_PROJECT, limit=50
    )
    assert [r["unit_id"] for r in rows], "the exclusion removed everything"


@pytest.mark.asyncio
async def test_search_keeps_rows_with_no_project_type(db):
    """The NULL trap, pinned.

    `project_type != 'reference'` is NULL — not TRUE — for an untyped row, so
    the obvious spelling of this exclusion drops every row that declares no
    partition. That failure is invisible: the endpoint keeps working and simply
    returns less, and nothing about a short list says it was filtered.
    """
    rows = await knowledge_crud.search_fts(
        db, "untyped", exclude_project=REFERENCE_PROJECT, limit=50
    )
    assert "kb-null" in [r["unit_id"] for r in rows], (
        "a row with project_type NULL was dropped by the exclusion"
    )


@pytest.mark.asyncio
async def test_stats_excludes_the_partition_and_keeps_null_rows(db):
    """The count must describe the set browsing can actually reach."""
    scoped = await knowledge_crud.stats(db, exclude_project=REFERENCE_PROJECT)
    unscoped = await knowledge_crud.stats(db)
    assert scoped["total"] == 2, f"expected the 2 non-reference rows, got {scoped['total']}"
    assert unscoped["total"] == 3, "the unscoped call must still see everything"
    assert REFERENCE_PROJECT not in str(scoped.get("domains", {})), (
        "a reference domain leaked into the knowledge stats breakdown"
    )


@pytest.mark.asyncio
async def test_exclusion_is_opt_in_so_references_py_is_unaffected(db):
    """`references.py` calls the same helpers and MUST still see its own rows.

    The fix must not close the reference store's own door while closing the
    knowledge browser's. Without this, a later 'simplify' that makes the
    exclusion unconditional would break the References tab and no other test
    here would notice.
    """
    rows = await knowledge_crud.search_fts(db, "Value", project=REFERENCE_PROJECT, limit=50)
    assert [r["unit_id"] for r in rows] == ["ref-1"], (
        "the reference store lost access to its own partition"
    )
