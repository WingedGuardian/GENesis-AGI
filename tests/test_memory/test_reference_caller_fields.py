"""Round-4 (PR #4338) fix: EVERY caller field a credential write can carry to a
store is checked against its value (#2426), not a hand-kept list.

Each of authority, provenance, memory_class, purpose and ingestion_source is
read as text (``field_text``: a str as is, anything else as JSON with non-ASCII
kept) and leaf by leaf (every string or scalar nested inside), so a non-string,
nested or fullwidth spelling hides nothing. A credential's memory_class becomes
an indexed tag, so it must also be a known class.

Fake credentials and example.org hosts only, never real secrets.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.mcp.memory.knowledge import _format_reference_body as fmt
from genesis.memory.knowledge_ingest import field_text
from tests.test_memory.test_reference_secret_class_fixes import _ingest, _PointStore


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _body(value: str) -> str:
    return fmt(
        kind="credentials",
        identifier="svc",
        description="Example service",
        value=value,
        tags=None,
        source=None,
    )


async def _refused(db, value: str, match: str, **kwargs) -> None:
    store = _PointStore()
    with pytest.raises(ValueError, match=match) as refused:
        await _ingest(db, store, _body(value), concept="svc", **kwargs)
    assert value not in str(refused.value)
    assert store.calls == []
    for table in ("knowledge_units", "knowledge_fts", "reference_secrets"):
        assert await db.execute_fetchall(f"SELECT 1 FROM {table}") == []  # noqa: S608


async def test_a_memory_class_repeating_the_value_is_refused(db):
    """Codex 4533893147: the class becomes a "class:..." tag the store indexes."""
    await _refused(db, "Demo!Fake123", "memory class", memory_class="Demo!Fake123")


async def test_an_unknown_memory_class_is_refused(db):
    await _refused(db, "password: Known!Fake1", "memory class", memory_class="ops-notes")


async def test_a_fullwidth_value_in_purpose_is_refused(db):
    """The purpose used to be checked JSON-escaped, which hid a fullwidth spelling."""
    await _refused(db, "password: Wide!Fake2", "purpose", purpose=["ops", "Ｗｉｄｅ！Ｆａｋｅ２"])


@pytest.mark.parametrize(
    ("value", "provenance"),
    [
        ("9137524680", {"source_doc": "x", "line": 9137524680}),  # a number
        ("password: Nest!Fake3", {"extra": {"note": "Nest!Fake3"}}),  # a dict holding it
        ("password: List!Fake4", {"items": ["a", "List!Fake4"]}),  # a list
        ('password: Qu"ote5x', {"extra": {"k": 'Qu"ote5x'}}),  # JSON would escape the quote
    ],
)
async def test_a_non_string_provenance_value_is_refused(db, value, provenance):
    await _refused(db, value, "provenance", provenance=provenance)


async def test_a_clean_credential_write_with_every_field_set_succeeds(db):
    store = _PointStore()
    unit = await _ingest(
        db,
        store,
        _body("password: Clean!Fake6"),
        concept="svc",
        authority="manual",
        memory_class="fact",
        purpose=["ops", "rotation"],
        ingestion_source="manual",
        provenance={
            "source_doc": "reference_store:manual",
            "platform": "manual",
            "line": 12,
            "extra": {"team": "infra", "hosts": ["db.example.org"]},
        },
    )
    assert await kc.get_reference_secret(db, unit) == "password: Clean!Fake6"
    assert store.calls[-1]["memory_class"] == "fact"


def test_field_text_reads_every_type():
    assert field_text(None) is None
    assert field_text("abc") == "abc"
    assert field_text(["ａ", 1]) == '["ａ", 1]'  # non-ASCII kept as written
    assert field_text({"k": object}).startswith('{"k": "<class')
