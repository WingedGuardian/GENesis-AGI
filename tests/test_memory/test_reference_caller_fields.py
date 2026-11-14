"""Round-4 (PR #4338) fix, reworked by #2426 PR A: EVERY caller field a
credential write can carry to a store is held with its value in the secret
row (owner ruling c91c5b74), and the write goes on (ruling 6f73fe4d).

Authority, provenance, purpose and ingestion_source never reach an indexed
store, whatever their type or spelling. A credential's memory_class becomes an indexed
tag, so an unknown one is stored as ``fact``.

Fake credentials and example.org hosts only, never real secrets.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.mcp.memory.knowledge import _format_reference_body as fmt
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


async def _masked(db, value: str, secret: str, **kwargs) -> dict:
    """The write succeeds; nothing indexed or stored beside the unit carries
    ``secret``; the value is held whole."""
    store = _PointStore()
    unit = await _ingest(db, store, _body(value), concept="svc", **kwargs)
    row = await kc.get(db, unit)
    fts = await db.execute_fetchall("SELECT * FROM knowledge_fts WHERE unit_id = ?", (unit,))
    for text in [*map(str, row.values()), *map(str, fts[0]), *map(str, store.calls)]:
        assert secret not in text, text
    assert await kc.get_reference_secret(db, unit) == value
    return {"row": row, "store": store}


async def test_a_memory_class_repeating_the_value_is_stored_as_fact(db):
    """Codex 4533893147: the class becomes a "class:..." tag the store indexes."""
    out = await _masked(db, "Demo!Fake123", "Demo!Fake123", memory_class="Demo!Fake123")
    assert out["store"].calls[-1]["memory_class"] == "fact"


async def test_an_unknown_memory_class_is_stored_as_fact(db):
    out = await _masked(db, "password: Known!Fake1", "Known!Fake1", memory_class="ops-notes")
    assert out["store"].calls[-1]["memory_class"] == "fact"


async def test_a_fullwidth_value_in_purpose_is_held_not_indexed(db):
    """The purpose used to be checked JSON-escaped, which hid a fullwidth
    spelling; now it is held with the value (owner ruling c91c5b74)."""
    from genesis.memory import reference_ops as ro

    purpose = ["ops", "Ｗｉｄｅ！Ｆａｋｅ２"]
    out = await _masked(db, "password: Wide!Fake2", "Ｗｉｄｅ！Ｆａｋｅ２", purpose=purpose)
    assert out["row"]["purpose"] is None
    assert (await ro.reveal_meta(db, out["row"]))["purpose"] == purpose


@pytest.mark.parametrize(
    ("value", "provenance", "secret"),
    [
        ("9137524680", {"source_doc": "x", "section_title": 9137524680}, "9137524680"),  # a number
        ("password: Nest!Fake3", {"extra": {"note": "Nest!Fake3"}}, "Nest!Fake3"),  # a dict
        ("password: List!Fake4", {"items": ["a", "List!Fake4"]}, "List!Fake4"),  # a list
        ('password: Qu"ote5x', {"extra": {"k": 'Qu"ote5x'}}, 'Qu"ote5x'),  # JSON escapes it
    ],
)
async def test_a_non_string_provenance_value_is_masked(db, value, provenance, secret):
    await _masked(db, value, secret, provenance=provenance)


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
