"""Tests for shared reference-store ops: body parser + delete helper.

The parser is the dashboard's security boundary — list/search/detail render
ONLY the parsed description, reveal returns ONLY the parsed value, and a body
this code didn't write fails CLOSED (empty). The contract test round-trips the
parser against BOTH real formatters (the reference_store MCP tool's and the
extraction-job's) so the two can't silently drift out of parser range.

Uses fake creds + TEST-NET (203.0.113.x) values only — never real secrets.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.mcp.memory.knowledge import _format_reference_body as fmt_mcp
from genesis.memory.reference_extraction import (
    _format_reference_body as fmt_extraction,
)
from genesis.memory.reference_ops import (
    REFERENCE_PROJECT,
    delete_reference_entry,
    parse_reference_body,
)

# ─── Parser contract: recover description + value from both formatters ────────

_CASES = [
    # (description, value)
    ("ScarletAndRage forum login for the 614buckeye persona", "admin / Hunter2!xyz"),
    ("Edge router admin panel", "https://203.0.113.10/admin"),
    ("Container host on the lab subnet", "203.0.113.42"),
    # value containing the separator + special chars
    ("Service account", "svc-bot / p@ss:w0rd/with slashes"),
    # description that mentions the word "Value" (must not confuse the parser)
    ("The Value field rotates monthly per policy", "rotating-token-XYZ"),
    # multi-line description
    ("First line of context.\nSecond line with more detail.", "secret-99"),
]


@pytest.mark.parametrize(("description", "value"), _CASES)
def test_parse_roundtrip_mcp_formatter(description, value):
    body = fmt_mcp(
        kind="credentials",
        identifier="Test Entry",
        description=description,
        value=value,
        tags=["forum", "persona:614buckeye"],
        source={"captured_via": "manual", "session_id": "sess123"},
    )
    parsed = parse_reference_body(body)
    assert parsed["value"] == value
    assert parsed["description"] == description.strip()
    # The value must NOT leak into the description field.
    assert value not in parsed["description"]


@pytest.mark.parametrize(("description", "value"), _CASES)
def test_parse_roundtrip_extraction_formatter(description, value):
    body = fmt_extraction(
        kind="credentials",
        identifier="Test Entry",
        description=description,
        value=value,
        tags=["auto"],
        session_id="sess123",
    )
    parsed = parse_reference_body(body)
    assert parsed["value"] == value
    assert parsed["description"] == description.strip()
    assert value not in parsed["description"]


def test_parse_no_tags_no_source():
    body = fmt_mcp(
        kind="url", identifier="Docs", description="API docs",
        value="https://example.com/docs", tags=None, source=None,
    )
    parsed = parse_reference_body(body)
    assert parsed["value"] == "https://example.com/docs"
    assert parsed["description"] == "API docs"


def test_parse_fails_closed_on_unknown_shape():
    # No canonical "\n\nValue: " marker → both fields empty (no leak).
    assert parse_reference_body("some legacy freeform body with a secret xyz") == {
        "description": "", "value": "",
    }
    assert parse_reference_body("") == {"description": "", "value": ""}
    assert parse_reference_body(None) == {"description": "", "value": ""}


def test_parse_value_only_no_trailer():
    # Body with Value but no Tags/Captured trailer.
    body = "[reference.fact] Pi\n\nMath constant\n\nValue: 3.14159"
    parsed = parse_reference_body(body)
    assert parsed["value"] == "3.14159"
    assert parsed["description"] == "Math constant"


# ─── Delete helper: SQLite + Qdrant + project_type guard ──────────────────────


@pytest.fixture
async def _db():
    async with aiosqlite.connect(":memory:") as conn:
        await create_all_tables(conn)
        await conn.commit()
        yield conn


async def _insert_ref(db, *, qdrant_id="qid-123", project_type=REFERENCE_PROJECT):
    return await kc.insert(
        db,
        project_type=project_type,
        domain="reference.credentials",
        source_doc="reference_store:manual",
        concept="Test Cred",
        body="[reference.credentials] Test Cred\n\nctx\n\nValue: admin / pw",
        qdrant_id=qdrant_id,
        source_pipeline="reference_store",
    )


async def test_delete_removes_row_and_qdrant(_db):
    unit_id = await _insert_ref(_db)
    store = AsyncMock()
    store.delete = AsyncMock()

    deleted = await delete_reference_entry(_db, store, unit_id)

    assert deleted is True
    store.delete.assert_awaited_once_with("qid-123")
    assert await kc.get(_db, unit_id) is None  # SQLite row gone
    # FTS row gone too
    rows = await _db.execute_fetchall(
        "SELECT COUNT(*) FROM knowledge_fts WHERE unit_id = ?", (unit_id,),
    )
    assert rows[0][0] == 0


async def test_delete_missing_returns_false(_db):
    store = AsyncMock()
    assert await delete_reference_entry(_db, store, "nonexistent") is False
    store.delete.assert_not_awaited()


async def test_delete_refuses_non_reference(_db):
    unit_id = await _insert_ref(_db, project_type="knowledge")
    store = AsyncMock()
    with pytest.raises(ValueError, match="not a reference entry"):
        await delete_reference_entry(_db, store, unit_id)
    # Row must survive a refused delete.
    assert await kc.get(_db, unit_id) is not None


async def test_delete_store_none_sqlite_only(_db):
    unit_id = await _insert_ref(_db)
    deleted = await delete_reference_entry(_db, None, unit_id)
    assert deleted is True
    assert await kc.get(_db, unit_id) is None
