"""Round-3 (PR #4338) fixes for the credential field check (#2426).

1. A structured value (a connection URL, a DSN, a key=value line) is probed by
   its fragments: percent-decoded, split on whitespace and ``: / @ ? & = # ; ,``,
   with host names, URL schemes and ports left out. Its password, token or key
   is a probe of its own.
2. The body header is rebuilt from the (masked) identifier; any other first
   line is part of the held-out description.
3. A caller tag keeps its text with the value's spans masked (strictly); a
   tag that was nothing but the value is dropped. The identifier is masked
   too, and the caller's metadata is held with the value; nothing refuses the
   write (#2426 PR A).

Corpus: the round-3 premise check's 12 fake leaks and 11 realistic accepts.
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
from tests.test_memory.test_reference_secret_class_fixes import _ingest, _PointStore

_PG = "postgresql://app:Demo%40Fake123@db.example.org/data"

# (name, value, the leaked spelling, where it is placed)
_LEAKS = [
    ("pg password tag", _PG, "Demo@Fake123", "tag"),
    ("redis password", "redis://:Rz9fakeKey77@cache.example.org:6379/0", "Rz9fakeKey77", "tag"),
    (
        "mongo password",
        "mongodb+srv://svc:Mg%2Ffake%2B99@cl0.example.net/db?retryWrites=true",
        "Mg/fake+99",
        "tag",
    ),
    ("amqp password", "amqp://guest:Aq7fakepw@mq.example.org:5672/vh1", "Aq7fakepw", "tag"),
    ("scheme-less", "app:Sl4fakepw@db.example.org", "Sl4fakepw", "tag"),
    (
        "token as user",
        "https://tok_FAKEtoken1234567890@git.example.org/org/repo.git",
        "tok_FAKEtoken1234567890",
        "tag",
    ),
    ("query secret", "https://api.example.org/v1?api_key=Qk8fakeKEY99&x=1", "Qk8fakeKEY99", "tag"),
    ("password in identifier", _PG, "Demo@Fake123", "identifier"),
    ("password in body header", _PG, "Demo@Fake123", "header"),
    (
        "dsn key=value form",
        "host=db.example.org user=app password=Kv5fakepw dbname=x",
        "Kv5fakepw",
        "tag",
    ),
    (
        "jdbc nested",
        "jdbc:postgresql://db.example.org/x?user=app&password=Jd6fakepw",
        "Jd6fakepw",
        "tag",
    ),
]
#: The stated limit: a secret that reads as a plain word is matched only as the
#: whole value, so it is not caught as a fragment.
_WORD_SECRET = (
    "all-letter password",
    "postgresql://app:hunter@db.example.org/data",
    "hunter",
    "tag",
)

# (name, value, identifier, tags kept, tags dropped)
_ACCEPTS = [
    ("identifier names host and scheme", _PG, "postgres db.example.org", [], []),
    ("tag host", _PG, "pg", ["db.example.org"], []),
    ("tag scheme word", _PG, "pg", ["postgres"], []),
    ("tag database name", _PG, "pg", ["data"], []),
    (
        "tag database with digit",
        "postgresql://app:pw9Fake@db.example.org/app_db1",
        "pg",
        ["prod"],
        ["app_db1"],
    ),
    ("tag vhost", "amqp://guest:Aq7fakepw@mq.example.org:5672/vh1", "mq", ["vh1"], []),
    (
        "tag host:port",
        "redis://:Rz9fakeKey77@cache.example.org:6379/0",
        "cache",
        ["cache.example.org:6379"],
        [],
    ),
    ("identifier names the login", "ForumUser42 / Fk9!pass77", "github ForumUser42", [], []),
    ("tag names the login", "ForumUser42 / Fk9!pass77", "github", ["forum"], ["ForumUser42"]),
    (
        "tag query flag",
        "https://api.example.org/v1?api_key=Qk8fakeKEY99&region=us-east-1",
        "api",
        ["api"],
        ["us-east-1"],
    ),
    (
        "tag repo path",
        "https://tok_FAKEtoken1234567890@git.example.org/org/repo.git",
        "git",
        ["repo.git"],
        [],
    ),
]


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _body(identifier: str, value: str, tags: list[str]) -> str:
    return fmt(
        kind="credentials",
        identifier=identifier,
        description="Example service",
        value=value,
        tags=tags or None,
        source=None,
    )


async def _write(db, store, value, identifier, tags, *, header=None):
    body = _body(header or identifier, value, tags)
    return await _ingest(
        db,
        store,
        body,
        concept=identifier,
        tags_json=json.dumps(["reference", "credentials", *tags]),
    )


async def _indexed(db, store, unit) -> list[str]:
    row = await kc.get(db, unit)
    fts = await db.execute_fetchall(
        "SELECT concept, body, tags FROM knowledge_fts WHERE unit_id = ?",
        (unit,),
    )
    texts = [row["body"], row["tags"], row["concept"], *fts[0], *store.points.values()]
    return texts + [json.dumps(call.get("tags")) for call in store.calls]


@pytest.mark.parametrize(("name", "value", "leak", "where"), _LEAKS, ids=[c[0] for c in _LEAKS])
async def test_every_leak_is_masked_or_its_tag_dropped(db, name, value, leak, where):
    store = _PointStore()
    if where == "identifier":
        unit = await _write(db, store, value, f"pg {leak}", [])
        assert (await kc.get(db, unit))["concept"].startswith("pg [held] #")
    elif where == "header":
        # A header that is not the one for the concept is held with the description.
        unit = await _write(db, store, value, "pg", [], header=f"pg {leak}")
        assert (await ro.reveal_reference(db, await kc.get(db, unit)))[1].startswith(
            f"[reference.credentials] pg {leak}"
        )
    else:
        unit = await _write(db, store, value, "svc", [leak, "keep-me"])
        assert json.loads((await kc.get(db, unit))["tags"]) == ["reference", "credentials", "keep-me"]
    for text in await _indexed(db, store, unit):
        assert leak not in text, (name, text)


async def test_a_secret_that_reads_as_a_word_is_matched_only_whole(db):
    """The stated limit (CURRENT.md): an all-letter secret inside a structured
    value is not secret-shaped, so a tag naming it alone stays."""
    _name, value, leak, _where = _WORD_SECRET
    store = _PointStore()
    unit = await _write(db, store, value, "svc", [leak])
    assert leak in json.loads((await kc.get(db, unit))["tags"])


@pytest.mark.parametrize(
    ("name", "value", "identifier", "kept", "dropped"),
    _ACCEPTS,
    ids=[c[0] for c in _ACCEPTS],
)
async def test_no_realistic_write_is_refused(db, name, value, identifier, kept, dropped):
    store = _PointStore()
    unit = await _write(db, store, value, identifier, kept + dropped)
    row = await kc.get(db, unit)
    assert json.loads(row["tags"]) == ["reference", "credentials", *kept], name
    for tag in dropped:
        assert all(tag not in text for text in await _indexed(db, store, unit)), tag
    assert row["concept"] == identifier


async def test_a_mismatched_header_is_held_with_the_description(db):
    """The header is rebuilt from the concept; any other first line is part of
    the description, held out with it."""
    value = "ForumUser42 / Fk9!pass77"
    store = _PointStore()
    unit = await _write(db, store, value, "forum ForumUser42", [], header="other ForumUser42")
    row = await kc.get(db, unit)
    assert row["body"].startswith("[reference.credentials] forum ForumUser42\n")
    assert "other" not in row["body"]


async def test_a_login_in_the_tags_trailer_is_dropped(db):
    """Round-3 M-2: tags are probed strictly, the login included."""
    store = _PointStore()
    body = _body("forum", "ForumUser42 / Fk9!pass77", ["ForumUser42", "forum"])
    unit = await _ingest(db, store, body, concept="forum")
    stored = (await kc.get(db, unit))["body"]
    assert stored.endswith("\nTags: forum")
    assert "ForumUser42" not in stored and "Fk9!pass77" not in stored


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("authority", {"authority": "pasted Demo@Fake123"}),
        ("provenance", {"provenance": {"source_doc": "notes Demo%40Fake123"}}),
    ],
)
async def test_metadata_is_masked(db, field, kwargs):
    store = _PointStore()
    unit = await _ingest(db, store, _body("pg", _PG, []), concept="pg", **kwargs)
    assert all("Demo@Fake123" not in str(call) and "Demo%40Fake123" not in str(call) for call in store.calls)
    assert "Fake123" not in str((await kc.get(db, unit)).values())


@pytest.mark.parametrize(
    "value",
    [
        "<password>Zq8vLm2x</password>",
        "svc|Zq8vLm2x",
        "DOM\\svc\\Zq8vLm2x",
        "user：Zq8vLm2x",
    ],
)
def test_xml_pipe_backslash_and_fullwidth_separators_split_the_value(value):
    """Security review MEDIUM (round 3): these separators were not in the split
    set, so a tag holding the bare secret passed."""
    assert ro.mask_value_spans(value, "tag Zq8vLm2x") == "tag [held]"
