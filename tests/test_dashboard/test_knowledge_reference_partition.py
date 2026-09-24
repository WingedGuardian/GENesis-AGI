"""The knowledge browser must not be a second door onto the reference store.

Both stores live in ``knowledge_units``. The reference store is the
``project_type='reference'`` partition, and a reference row carries its stored
value in ``body`` — credentials, network facts and account details among them.

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


# ── Route-level guards ───────────────────────────────────────────────
#
# The CRUD tests above prove the SQL excludes the partition. They say nothing
# about whether the ROUTES apply it — a regression in `recent`, `detail` or
# `delete` passes every test above. These cover the route layer for all FIVE
# routes rather than the three a reviewer named, because the population is the
# module's routes, not the subset that was flagged.

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from flask import Flask  # noqa: E402

# Imported for the SIDE EFFECT of registering the routes on the shared
# blueprint — without it the test client 404s on every path below, which
# would read as a passing guard.
import genesis.dashboard.routes.knowledge  # noqa: E402,F401
import genesis.dashboard.routes.knowledge_upload  # noqa: E402,F401
from genesis.dashboard._blueprint import blueprint  # noqa: E402


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


def _rt(db=None):
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.db = db if db is not None else MagicMock()
    return rt


def _reference_row():
    return {"id": "ref-1", "project_type": REFERENCE_PROJECT, "body": f"Value: {_SENTINEL}"}


def test_route_recent_excludes_the_partition_in_its_sql(client):
    """`recent` builds its SQL inline, so the guard lives in the route itself."""
    cursor = MagicMock()
    cursor.fetchall = AsyncMock(return_value=[])
    cursor.fetchone = AsyncMock(return_value=(0,))
    cursor.description = [("id",)]
    db = MagicMock()
    db.execute = AsyncMock(return_value=cursor)

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _rt(db)
        assert client.get("/api/genesis/knowledge/recent").status_code == 200

    # BOTH statements — the page and the total — must be scoped, or paging
    # walks off the end of a list shorter than the total it was handed.
    assert db.execute.await_count == 2, "expected a page query and a total query"
    for call in db.execute.await_args_list:
        sql, params = call.args[0], call.args[1]
        assert "project_type IS NULL OR project_type != ?" in sql, f"unscoped SQL: {sql}"
        assert REFERENCE_PROJECT in params, f"exclusion value not bound: {params}"


def test_route_detail_refuses_a_reference_row(client):
    """404, and the body must not carry the value even in an error path."""
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge.get",
            new_callable=AsyncMock, return_value=_reference_row(),
        ),
    ):
        MockRT.instance.return_value = _rt()
        resp = client.get("/api/genesis/knowledge/ref-1")

    assert resp.status_code == 404
    leaked = resp.get_data(as_text=True).count(_SENTINEL)
    assert leaked == 0, "the refused response carried the reference value"


def test_route_detail_still_serves_ordinary_knowledge(client):
    """Guard-the-guard: a 404 for everything would satisfy the test above."""
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge.get",
            new_callable=AsyncMock,
            return_value={"id": "kb-1", "project_type": "genesis", "body": "ordinary"},
        ),
    ):
        MockRT.instance.return_value = _rt()
        resp = client.get("/api/genesis/knowledge/kb-1")

    assert resp.status_code == 200


def test_route_delete_refuses_a_reference_row_and_does_not_delete(client):
    """The destructive half.

    Asserting the 404 alone is not enough: the route reads the row BEFORE
    deleting, so a guard placed after the delete would still answer 404 having
    already destroyed a stored credential. The load-bearing assertion is that
    `delete` was never awaited.
    """
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge.get",
            new_callable=AsyncMock, return_value=_reference_row(),
        ),
        patch("genesis.db.crud.knowledge.delete", new_callable=AsyncMock) as mock_delete,
    ):
        MockRT.instance.return_value = _rt()
        resp = client.delete("/api/genesis/knowledge/ref-1")

    assert resp.status_code == 404
    mock_delete.assert_not_awaited()


def test_route_search_refuses_an_explicit_reference_project(client):
    """`?project=reference` was the shortest path to the whole store."""
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _rt()
        resp = client.get(f"/api/genesis/knowledge/search?q=Value&project={REFERENCE_PROJECT}")

    assert resp.status_code == 400
    assert "references" in (resp.get_json() or {}).get("use", "")


def test_route_search_passes_the_exclusion_to_the_query(client):
    """An ordinary search must still carry the exclusion down to the SQL."""
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge.search_fts",
            new_callable=AsyncMock, return_value=[],
        ) as mock_search,
    ):
        MockRT.instance.return_value = _rt()
        assert client.get("/api/genesis/knowledge/search?q=anything").status_code == 200

    assert mock_search.await_args.kwargs.get("exclude_project") == REFERENCE_PROJECT


def test_route_stats_passes_the_exclusion_to_the_query(client):
    """The count must describe the set browsing can actually reach."""
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge.stats",
            new_callable=AsyncMock,
            return_value={"total": 0, "by_domain": {}, "by_tier": {}},
        ) as mock_stats,
    ):
        MockRT.instance.return_value = _rt()
        assert client.get("/api/genesis/knowledge/stats").status_code == 200

    assert mock_stats.await_args.kwargs.get("exclude_project") == REFERENCE_PROJECT


@pytest.mark.asyncio
async def test_stats_composes_both_filters_rather_than_dropping_one(db):
    """`project` and `exclude_project` must AND, matching `search_fts`.

    An `elif` here silently ignores the exclusion whenever both are supplied,
    so the two sibling helpers would disagree about what the same pair of
    arguments means. No caller passes both today — the inconsistency is the
    defect, because the next caller will not know which one it got.
    """
    both = await knowledge_crud.stats(
        db, project=REFERENCE_PROJECT, exclude_project=REFERENCE_PROJECT
    )
    assert both["total"] == 0, (
        "the exclusion was dropped when a project filter was also supplied"
    )

    # Guard-the-guard: a WHERE that matched nothing regardless would pass the above.
    only_project = await knowledge_crud.stats(db, project=REFERENCE_PROJECT)
    assert only_project["total"] == 1, "the project filter alone stopped working"


# ── The partition has exactly one writer ─────────────────────────────


def test_upload_refuses_the_reference_partition(client):
    """A document upload must not be able to land in the reference store.

    Excluding the partition from the knowledge browser closed the last door on
    a row that should never have been there: `parse_reference_body` fails
    CLOSED on a body it did not write and never falls back to the raw text, so
    the References browser renders such a row blank, and the knowledge browser
    now excludes it outright. The document would be reachable through neither —
    data loss wearing the shape of a successful upload.

    The fix is at the boundary rather than in either browser: the partition has
    one writer, and it is not this route.
    """
    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = _rt()
        resp = client.post(
            "/api/genesis/knowledge/ingest",
            json={"upload_id": "u-1", "project_type": REFERENCE_PROJECT, "domain": "auto"},
        )

    assert resp.status_code == 400
    assert REFERENCE_PROJECT in (resp.get_json() or {}).get("error", "")


def test_upload_still_accepts_an_ordinary_project_type(client):
    """Guard-the-guard: refusing every upload would satisfy the test above.

    Proven by REACHING THE NEXT STEP rather than by inspecting an error string:
    the partition guard sits immediately before the status transition, so the
    transition being attempted is exactly what "validation let it through"
    means. Asserting on the absence of an error message would also pass if the
    route fell over for some unrelated reason.
    """
    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.knowledge_uploads.atomic_transition",
            new_callable=AsyncMock, return_value=False,
        ) as mock_transition,
        # The route consults `get` on the did-not-transition branch; stubbed so
        # this test exercises the GUARD rather than the ingest machinery below it.
        patch(
            "genesis.db.crud.knowledge_uploads.get",
            new_callable=AsyncMock, return_value=None,
        ),
    ):
        MockRT.instance.return_value = _rt()
        client.post(
            "/api/genesis/knowledge/ingest",
            json={"upload_id": "u-1", "project_type": "genesis", "domain": "auto"},
        )

    mock_transition.assert_awaited()
    assert mock_transition.await_args.kwargs.get("project_type") == "genesis"


@pytest.mark.asyncio
async def test_taxonomy_does_not_offer_the_reserved_partition(db):
    """The UI must not autocomplete a value the API refuses.

    `taxonomy` reads DISTINCT project_type straight from `knowledge_units`,
    which contains the reference rows — so the upload form was actively
    offering the one value that orphaned the document.
    """
    from genesis.db.crud import knowledge_uploads

    tax = await knowledge_uploads.taxonomy(db)
    assert REFERENCE_PROJECT not in tax["project_types"], (
        "autocomplete still offers the reserved partition"
    )
    assert "genesis" in tax["project_types"], "ordinary project types stopped being offered"
