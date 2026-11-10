"""Round-1 class fixes for held-out credential values (#2426).

A. A credential write never reuses a vector point, and a point a write created
   that no unit names when the write ends is deleted, so no cleanup deletes a
   point a unit names and no refused write leaves a recallable orphan.
B. A reader returns a body and a value of the same write, even when the unit
   was repointed after its row was read.
C. The hold-out scrubs with the stored secret when a re-ingest carries only the
   placeholder, an identifier label exempts only its own first token, and a
   postcondition independent of the parser refuses a write whose indexed
   fields still carry the value.

Fake credentials and example.org hosts only, never real secrets.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.mcp.memory.knowledge import _format_reference_body as fmt
from genesis.memory import reference_ops as ro
from genesis.memory.knowledge_ingest import ingest_knowledge_unit

_KEY = "Example portal admin"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


class _PointStore:
    """A store that deduplicates exact content the way MemoryStore does (unless
    ``dedup=False``) and keeps the set of live points, so a test can see which
    points a cleanup deleted and which a unit names."""

    def __init__(self):
        self.points: dict[str, str] = {}
        self.calls: list[dict] = []
        self._embeddings = SimpleNamespace(model_name="test-model")
        self.before_delete = None  # async hook(point_id)

    async def store_reporting_creation(self, content, source, *, dedup=True, **kwargs):
        self.calls.append({"content": content, "dedup": dedup, **kwargs})
        if dedup:
            for point_id, stored in self.points.items():
                if stored == content:
                    return point_id, False
        point_id = str(uuid.uuid4())
        self.points[point_id] = content
        return point_id, True

    async def store(self, content, source, **kwargs):
        return (await self.store_reporting_creation(content, source, **kwargs))[0]

    async def delete(self, point_id):
        if self.before_delete is not None:
            await self.before_delete(point_id)
        self.points.pop(point_id, None)
        return {}


def _body(description: str, value: str, *, kind: str = "credentials", tags=None) -> str:
    return fmt(
        kind=kind,
        identifier=_KEY,
        description=description,
        value=value,
        tags=tags,
        source=None,
    )


async def _ingest(db, store, body, *, domain="reference.credentials", concept=_KEY, **kw):
    return await ingest_knowledge_unit(
        store=store,
        db=db,
        content=body,
        project="reference",
        domain=domain,
        concept=concept,
        collection="episodic_memory",
        memory_type="episodic",
        **kw,
    )


async def _named_points(db) -> set[str]:
    rows = await db.execute_fetchall(
        "SELECT qdrant_id FROM knowledge_units WHERE qdrant_id IS NOT NULL"
    )
    return {r[0] for r in rows}


# ─── A: credential writes never reuse a point ───────────────────────────────


async def test_two_interleaved_same_key_writes_never_delete_the_live_point(db):
    """4521795434: A replaces P with P2 and pauses at the delete of P; B writes
    the ORIGINAL held body meanwhile. With point reuse B's unit would name P
    and A's resumed delete would kill it. Now B gets a point of its own."""
    store = _PointStore()
    unit = await _ingest(db, store, _body("Example portal login", "password: First!Fake1"))
    first_point = (await kc.get(db, unit))["qdrant_id"]

    entered, resume = asyncio.Event(), asyncio.Event()

    async def _pause_on_first_point(point_id):
        if point_id == first_point and not entered.is_set():
            entered.set()
            await resume.wait()

    store.before_delete = _pause_on_first_point
    writer_a = asyncio.create_task(
        _ingest(db, store, _body("Example portal login, rotated", "password: Second!Fake2"))
    )
    await entered.wait()  # A sits between its check and its delete of P
    await _ingest(db, store, _body("Example portal login", "password: Third!Fake3"))
    resume.set()
    await writer_a

    row = await kc.get(db, unit)
    assert row["qdrant_id"] in store.points, "the unit names a deleted point"
    # And no point is left recallable that no unit names.
    assert set(store.points) == await _named_points(db)
    assert await kc.get_reference_secret(db, unit) == "password: Third!Fake3"


@pytest.mark.parametrize(
    "error",
    [
        ValueError("refusing a credential reference with no value to hold (#2426)"),
        RuntimeError("the database went away"),
    ],
)
async def test_a_refused_upsert_leaves_no_orphan_point(db, monkeypatch, error):
    """The trigger's refusal (or any failure) after the vector was stored: the
    point this write created is deleted, since no unit names it."""
    store = _PointStore()

    async def _refuse(*_a, **_k):
        raise error

    monkeypatch.setattr(kc, "upsert", _refuse)
    with pytest.raises(type(error)):
        await _ingest(db, store, _body("Example portal login", "password: Refused!Fake4"))
    assert store.points == {}


async def test_a_cancelled_upsert_leaves_no_orphan_point(db, monkeypatch):
    store = _PointStore()

    async def _cancel(*_a, **_k):
        raise asyncio.CancelledError

    monkeypatch.setattr(kc, "upsert", _cancel)
    with pytest.raises(asyncio.CancelledError):
        await _ingest(db, store, _body("Example portal login", "password: Cancel!Fake5"))
    assert store.points == {}


async def test_a_credential_never_reuses_and_never_deletes_a_plain_memory(db, monkeypatch):
    """A plain memory with the very content of a credential's held body: the
    credential gets a point of its own, and its refusal deletes only that one."""
    store = _PointStore()
    body = _body("Example portal login", "password: Plain!Fake6")
    held, _, _, _ = ro.split_secret(body)
    plain_id, _ = await store.store_reporting_creation(held, "elsewhere")

    async def _refuse(*_a, **_k):
        raise ValueError("refused")

    monkeypatch.setattr(kc, "upsert", _refuse)
    with pytest.raises(ValueError):
        await _ingest(db, store, body)
    assert store.calls[-1]["dedup"] is False
    assert set(store.points) == {plain_id}


async def test_an_insert_only_dedup_hit_on_a_plain_memory_is_never_deleted(db):
    """A non-credential insert-only write that lost its key and deduplicated to
    an existing memory: that memory is not this write's, so it stays."""
    store = _PointStore()
    await _ingest(
        db,
        store,
        _body("Example portal", "https://portal.example.org", kind="url"),
        domain="reference.url",
    )
    other = _body("Example portal, moved", "https://new.example.org", kind="url")
    plain_id, _ = await store.store_reporting_creation(other, "elsewhere")  # no unit names it
    await _ingest(db, store, other, domain="reference.url", insert_only=True)
    assert plain_id in store.points
    assert store.calls[-1]["dedup"] is True


async def test_control_a_non_credential_write_still_dedups(db):
    store = _PointStore()
    body = _body("Example portal", "https://portal.example.org", kind="url")
    first = await _ingest(db, store, body, domain="reference.url", concept="first")
    second = await _ingest(db, store, body, domain="reference.url", concept="second")
    assert (await kc.get(db, first))["qdrant_id"] == (await kc.get(db, second))["qdrant_id"]
    assert len(store.points) == 1
    assert all(call["dedup"] is True for call in store.calls)


# ─── B: one statement reads the pair ────────────────────────────────────────


async def test_a_repoint_after_hydration_returns_the_new_writes_pair(db):
    """4521822692: the row is read, then reference_store rewrites the
    description and the value. The composed body and value are both the new
    write's, never the old description beside the new password."""
    store = _PointStore()
    unit = await _ingest(db, store, _body("Login username: alice", "password: Alice!Fake7"))
    snapshot = await kc.get(db, unit)
    await _ingest(db, store, _body("Login username: bob", "password: Bob!Fake8"))

    body, missing = await ro.compose_body(db, snapshot)
    assert missing is False
    assert "username: bob" in body
    assert "Value: password: Bob!Fake8" in body
    assert "alice" not in body
    assert await ro.resolve_value(db, snapshot) == "password: Bob!Fake8"

    without_ref = {k: v for k, v in dict(snapshot).items() if k != "secret_ref"}
    body, _ = await ro.compose_body(db, without_ref)
    assert "username: bob" in body
    assert "Value: password: Bob!Fake8" in body


async def test_control_no_repoint_composes_the_rows_own_pair(db):
    store = _PointStore()
    unit = await _ingest(db, store, _body("Login username: alice", "password: Alice!Fake7"))
    snapshot = await kc.get(db, unit)
    body, missing = await ro.compose_body(db, snapshot)
    assert missing is False
    assert "username: alice" in body
    assert "Value: password: Alice!Fake7" in body


async def test_get_reference_pair_reads_body_and_value_together(db):
    store = _PointStore()
    unit = await _ingest(db, store, _body("Example portal login", "password: Pair!Fake9"))
    body, value = await kc.get_reference_pair(db, unit)
    assert ro.SECRET_PLACEHOLDER in body
    assert value == "password: Pair!Fake9"
    assert await kc.get_reference_pair(db, "no-such-unit") is None


# ─── C: the hold-out uses the stored secret and checks its own result ───────


@pytest.mark.parametrize(
    "value",
    [
        "user: admin password: Shape!Fake10",
        "username=admin password=Shape!Fake10",
        "email: ops@example.org pass Shape!Fake10",
    ],
)
def test_an_identifier_label_exempts_only_its_own_first_token(value):
    """4521822679: what follows the identifier in the segment is scanned too."""
    assert "Shape!Fake10" in ro.secret_fragments(value)
    held, _, tags, concept = ro.split_secret(
        _body("sign in with Shape!Fake10", value),
        json.dumps(["portal", "Shape!Fake10"]),
        "portal Shape!Fake10",
    )
    for text in (held, tags, concept):
        assert "Shape!Fake10" not in text


def test_the_identifier_itself_stays_searchable():
    held, _, _, _ = ro.split_secret(
        _body("portal user example-admin", "user: example-admin password: Ident!Fake11")
    )
    assert "portal user example-admin" in held


async def test_a_placeholder_re_ingest_scrubs_with_the_stored_secret(db):
    """4521795647: a re-ingest carrying only the placeholder whose description,
    tags, trailer and authority repeat the stored password indexes none of it."""
    store = _PointStore()
    stored = "user: example-admin, password: Stored!Fake12"
    unit = await _ingest(db, store, _body("Example portal login", stored))
    held = _body(
        "rotated note: Stored!Fake12",
        ro.SECRET_PLACEHOLDER,
        tags=["portal", "Stored!Fake12"],
    )
    again = await _ingest(
        db,
        store,
        held,
        tags_json=json.dumps(["portal", "Stored!Fake12"]),
        authority="note Stored!Fake12",
        provenance={"source_doc": "doc Stored!Fake12"},
    )
    assert again == unit
    row = await kc.get(db, unit)
    fts = await db.execute_fetchall(
        "SELECT body, tags FROM knowledge_fts WHERE unit_id = ?", (unit,)
    )
    payload = store.calls[-1]
    for text in (
        row["body"],
        row["tags"],
        row["concept"],
        row["source_doc"],
        fts[0][0],
        fts[0][1],
        payload["content"],
        json.dumps(payload["tags"]),
    ):
        assert "Stored!Fake12" not in text, text
    assert await kc.get_reference_secret(db, unit) == stored


def test_split_secret_scrubs_a_placeholder_body_with_the_stored_value():
    held, value, tags, concept = ro.split_secret(
        _body("note Stored!Fake13", ro.SECRET_PLACEHOLDER),
        json.dumps(["Stored!Fake13"]),
        "portal Stored!Fake13",
        stored_value="password: Stored!Fake13",
    )
    assert value is None
    for text in (held, tags, concept):
        assert "Stored!Fake13" not in text
    assert f"Value: {ro.SECRET_PLACEHOLDER}" in held


@pytest.mark.parametrize(
    ("field", "kwargs", "description"),
    [
        ("body", {}, "sign in with Missed!Fake14"),
        ("tags", {"tags_json": json.dumps(["Missed!Fake14"])}, "Example portal login"),
        ("authority", {"authority": "via Missed!Fake14"}, "Example portal login"),
        ("provenance", {"provenance": {"platform": "Missed!Fake14"}}, "Example portal login"),
    ],
)
async def test_the_postcondition_refuses_what_the_parser_missed(
    db,
    monkeypatch,
    field,
    kwargs,
    description,
):
    """With the parser blinded (no fragments), the value still in an indexed
    field refuses the write before any store is touched; the message names the
    field, never the value."""
    monkeypatch.setattr(
        ro,
        "_fragments",
        lambda _v: ro._Fragments([], frozenset(), frozenset()),
    )
    store = _PointStore()
    with pytest.raises(ValueError, match=field) as refused:
        await _ingest(db, store, _body(description, "password: Missed!Fake14"), **kwargs)
    assert "Missed!Fake14" not in str(refused.value)
    assert store.calls == []
    assert store.points == {}
    for table in ("knowledge_units", "knowledge_fts", "reference_secrets"):
        assert await db.execute_fetchall(f"SELECT 1 FROM {table}") == []  # noqa: S608


async def test_the_postcondition_lets_an_identifier_through(db):
    """No false refusal: a description naming the login is not a leak."""
    store = _PointStore()
    unit = await _ingest(
        db,
        store,
        _body("portal user example-admin", "user: example-admin, password: Ok!Fake15"),
    )
    assert "example-admin" in (await kc.get(db, unit))["body"]


# ─── security review: identifier positions, query secrets, normalisation ────

_LEAK = "Leak!Fake20x"
_IDENTIFIER_POSITION_SHAPES = [
    (f"login: {_LEAK}", _LEAK),
    (f"email: {_LEAK}", _LEAK),
    (f"account: {_LEAK}", _LEAK),
    ("url: https://portal.example.org/?api_key=QueryFake21", "QueryFake21"),
    (f"{_LEAK} / forumuser", _LEAK),  # "secret / login"
]


@pytest.mark.parametrize(("value", "secret"), _IDENTIFIER_POSITION_SHAPES)
def test_a_secret_in_an_identifier_position_is_held(value, secret):
    held, _, tags, _ = ro.split_secret(
        _body(f"sign in with {secret}", value), json.dumps([secret]),
    )
    assert secret not in held
    assert secret not in tags


@pytest.mark.parametrize(("value", "secret"), _IDENTIFIER_POSITION_SHAPES)
async def test_the_postcondition_catches_an_identifier_position_secret(
    db, monkeypatch, value, secret,
):
    monkeypatch.setattr(ro, "_fragments", lambda _v: ro._Fragments([], frozenset(), frozenset()))
    store = _PointStore()
    with pytest.raises(ValueError, match="body"):
        await _ingest(db, store, _body(f"sign in with {secret}", value))
    assert store.points == {}


@pytest.mark.parametrize(
    "variant",
    [
        "Ｎｏｒｍ！Ｆａｋｅ２２",  # fullwidth
        "Norm!­Fake22",  # soft hyphen inserted
        "Norm!Fa​ke22",  # zero-width space inserted
    ],
)
async def test_the_postcondition_matches_normalised_text(db, variant):
    """A fullwidth, soft-hyphenated or zero-width-split repeat of the password
    is the password: the write is refused rather than indexed."""
    store = _PointStore()
    with pytest.raises(ValueError, match="body"):
        await _ingest(db, store, _body(f"sign in with {variant}", "password: Norm!Fake22"))
    assert store.points == {}


@pytest.mark.parametrize(
    "value",
    ["ForumUser42 / Pair!Fake23", "login: ForumUser42, password: Pair!Fake23"],
)
async def test_a_plain_login_in_an_identifier_position_stays_searchable(db, value):
    store = _PointStore()
    unit = await _ingest(db, store, _body("forum login ForumUser42", value))
    assert "forum login ForumUser42" in (await kc.get(db, unit))["body"]
