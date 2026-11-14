"""A credential's description is held out WITH its value (#2426, owner ruling).

No parser can scrub free text of a value it repeats in every spelling and
position, so the credential body that every indexed store sees carries only the
header (the identifier), a detail placeholder, the value placeholder and the
Tags/Captured trailer. The description and the value go to ONE reference_secrets
row (``detail``, ``value``); lookup and reveal put both back. In the other
indexed fields (identifier, tags, authority, provenance) every span of the value
is masked and the write goes on (#2426 PR A).

Fake credentials and example.org hosts only, never real secrets.
"""

from __future__ import annotations

import json

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.mcp.memory.knowledge import _format_reference_body as fmt
from genesis.memory import reference_ops as ro
from tests.test_memory.test_reference_secret_class_fixes import _KEY, _ingest, _PointStore


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _body(description: str, value: str, *, tags=None, identifier: str = _KEY) -> str:
    """A credential body whose header names ``identifier``: pass the test's
    concept, so the header is the one the ingest expects for it."""
    return fmt(
        kind="credentials",
        identifier=identifier,
        description=description,
        value=value,
        tags=tags,
        source=None,
    )


_HELD_SHAPE = (
    f"[reference.credentials] {_KEY}\n\n{ro.DETAIL_PLACEHOLDER}\n\nValue: {ro.SECRET_PLACEHOLDER}"
)

# Descriptions from the round-2 review and class audit that leaked through the
# old scrub, each with its value.
_LEAKY = [
    ("postgresql://app:Demo%40Fake123@db.example.org/data", "password is Demo@Fake123"),
    ("https://api.example.org/v1?token=abc%2Fdef%2B123", "token abc/def+123"),
    ("password: Demo@Fake123", "url postgresql://app:Demo%40Fake123@db.example.org"),
    ("https://api.example.org/v1?password=Fake+Pass9", "the password is Fake Pass9"),
    ("postgresql://app:Demo%2540Fake123@db.example.org/data", "password Demo%40Fake123"),
    ('password: Fa"ke9Pw', 'cfg {"pw": "Fa\\"ke9Pw"}'),
    ("password: Fake<Pw&9", "pw is Fake&lt;Pw&amp;9"),
    (
        "https://tok_FAKEfake0000FAKEfake0000FAKE00@git.example.org/o/r.git",
        "token tok_FAKEfake0000FAKEfake0000FAKE00",
    ),
    (
        "https://hooks.example.org/services/T0FAKE/B0FAKE/FakeFakeFake0000Fake",
        "secret part FakeFakeFake0000Fake",
    ),
    ("https://api.example.org/v1?auth=FakeAuth9x00", "auth FakeAuth9x00"),
]


async def _indexed(db, store, unit: str) -> list[str]:
    row = await kc.get(db, unit)
    fts = await db.execute_fetchall(
        "SELECT concept, body, tags FROM knowledge_fts WHERE unit_id = ?",
        (unit,),
    )
    return [row["body"], row["concept"], row["tags"], *fts[0], *store.points.values()]


@pytest.mark.parametrize(("value", "description"), _LEAKY)
async def test_no_indexed_store_sees_the_description_or_the_value(db, value, description):
    store = _PointStore()
    unit = await _ingest(db, store, _body(description, value))
    row = await kc.get(db, unit)
    assert row["body"] == _HELD_SHAPE  # by construction, whatever the description says
    assert list(store.points.values()) == [_HELD_SHAPE]
    for text in await _indexed(db, store, unit):
        assert description not in text
        assert value not in text


async def test_lookup_and_reveal_return_the_description_and_value_byte_for_byte(db):
    store = _PointStore()
    description = "Login for the example portal\nsecond line: Byte!Fake1 note"
    original = _body(description, "user: example-admin, password: Byte!Fake1", tags=["portal"])
    unit = await _ingest(db, store, original, tags_json=json.dumps(["portal"]))
    row = await kc.get(db, unit)
    assert ro.DETAIL_PLACEHOLDER in row["body"]
    composed, missing = await ro.compose_body(db, row)
    assert missing is False
    assert composed == original
    assert await ro.reveal_reference(db, row) == (
        "user: example-admin, password: Byte!Fake1",
        description,
    )
    assert await ro.resolve_value(db, row) == "user: example-admin, password: Byte!Fake1"


async def _secret_rows(db) -> list[tuple]:
    return [
        tuple(r) for r in await db.execute_fetchall("SELECT value, detail FROM reference_secrets")
    ]


async def test_a_placeholder_re_ingest_keeps_the_stored_value_and_description(db):
    store = _PointStore()
    unit = await _ingest(db, store, _body("Original note", "password: Keep!Fake2"))
    ref = (await kc.get(db, unit))["secret_ref"]
    await _ingest(db, store, _body(ro.DETAIL_PLACEHOLDER, ro.SECRET_PLACEHOLDER))
    assert (await kc.get(db, unit))["secret_ref"] == ref  # no new secret row
    assert await _secret_rows(db) == [("password: Keep!Fake2", "Original note")]


async def test_a_new_description_with_the_value_placeholder_keeps_the_value(db):
    """The new description, the old value, together as a NEW secret row."""
    store = _PointStore()
    unit = await _ingest(db, store, _body("Original note", "password: Keep!Fake3"))
    ref = (await kc.get(db, unit))["secret_ref"]
    await _ingest(db, store, _body("Rewritten note", ro.SECRET_PLACEHOLDER))
    assert (await kc.get(db, unit))["secret_ref"] != ref
    assert await _secret_rows(db) == [("password: Keep!Fake3", "Rewritten note")]


async def test_a_new_value_with_the_description_placeholder_keeps_the_description(db):
    store = _PointStore()
    await _ingest(db, store, _body("Original note", "password: Old!Fake4"))
    await _ingest(db, store, _body(ro.DETAIL_PLACEHOLDER, "password: New!Fake4"))
    assert await _secret_rows(db) == [("password: New!Fake4", "Original note")]


@pytest.mark.parametrize(
    "tag",
    [
        "Field!Fake5",
        "Field%21Fake5",  # percent-encoded
        "Ｆｉｅｌｄ！Ｆａｋｅ５",  # fullwidth
    ],
)
async def test_a_tag_carrying_the_value_is_dropped_whole(db, tag):
    """A tag that is nothing but the value has no other text to keep."""
    store = _PointStore()
    unit = await _ingest(
        db, store, _body("Example portal", "password: Field!Fake5"),
        tags_json=json.dumps(["portal", tag]),
    )
    assert json.loads((await kc.get(db, unit))["tags"]) == ["portal"]


@pytest.mark.parametrize(
    ("column", "kwargs"),
    [
        ("source_doc", {"provenance": {"source_doc": "notes Field!Fake5"}}),
        ("source_pipeline", {"provenance": {"source_pipeline": "pasted Field!Fake5"}}),
    ],
)
async def test_a_value_in_another_indexed_field_is_held_not_indexed(db, column, kwargs):
    """Owner ruling c91c5b74: the caller's metadata is held with the value."""
    store = _PointStore()
    unit = await _ingest(
        db, store, _body("Example portal", "password: Field!Fake5"), authority="pasted Field!Fake5", **kwargs,
    )
    row = await kc.get(db, unit)
    assert "Field!Fake5" not in str(row[column])
    assert all("Field!Fake5" not in str(call) for call in store.calls)
    assert (await ro.reveal_meta(db, row))["provenance"] == kwargs["provenance"]


async def test_a_clean_write_is_not_refused(db):
    store = _PointStore()
    unit = await _ingest(
        db,
        store,
        _body("Example portal, password Clean!Fake6 rotated monthly", "password: Clean!Fake6"),
        tags_json=json.dumps(["portal", "example.org"]),
        authority="manual",
        provenance={"source_doc": "reference_store:manual", "platform": "manual"},
    )
    assert await kc.get_reference_secret(db, unit) == "password: Clean!Fake6"


async def test_an_identifier_naming_the_login_of_a_pair_stays_and_its_secret_is_masked(db):
    """Capture writes "login / secret": the login may appear in the identifier
    and tags; the secret half still may not."""
    store = _PointStore()
    unit = await _ingest(
        db, store, _body("Forum account", "ForumUser42 / Fk9!pass77", identifier="forum ForumUser42"),
        concept="forum ForumUser42",
    )
    # The header names the login too, and is checked by the same rule (4531815540).
    assert (await kc.get(db, unit))["body"].startswith("[reference.credentials] forum ForumUser42\n")
    # The secret half is masked, under a tagged key.
    other = await _ingest(
        db, store, _body("Forum account", "ForumUser43 / Fk9!pass88", identifier="forum Fk9!pass88"),
        concept="forum Fk9!pass88",
    )
    assert (await kc.get(db, other))["concept"].startswith("forum [held] #")


def test_a_value_line_shaped_like_a_trailer_is_held_out_whole():
    """Security review MEDIUM-1: a value line starting 'Tags: ' would be read
    back as trailer. The writer's own value says where it ends; without it, a
    repeated trailer line holds out everything after the marker (#2426 PR A)."""
    value = "pw1\nTags: Zx9!kQ7mLeak"
    body = fmt(kind="credentials", identifier="svc", description="d", value=value, tags=["a"], source=None)
    held = ro.split_secret(body, value=value)
    assert held.value == value and held.body.endswith("\nTags: a")
    held = ro.split_secret(body)
    assert "Zx9!kQ7mLeak" not in held.body and held.value == value + "\nTags: a"


@pytest.mark.parametrize("value", ["hunter2xyz / Fk3a9!Zq", "Fk3a9Zq / w0rd!x99"])
async def test_tags_probe_both_halves_of_a_pair(db, value):
    """Security review MEDIUM-2, and round-3 M-2: tags are probed with the
    fragments of both halves (only the identifier exempts the login); a tag
    that is wholly one half is dropped, one carrying it keeps its other text."""
    store = _PointStore()
    login, secret = value.split(" / ")
    tags = [login, f"{secret}-old", "portal"]
    unit = await _ingest(
        db, store, _body("acct", value, tags=tags, identifier="acct portal"),
        concept="acct portal", tags_json=json.dumps(tags),
    )
    row = await kc.get(db, unit)
    assert json.loads(row["tags"]) == ["[held]-old", "portal"]
    assert row["body"].endswith("\nTags: [held]-old, portal")
