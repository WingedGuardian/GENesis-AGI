"""Round-1 class fixes for held-out credential values (#2426).

A. A credential write never reuses a vector point, and a point a write created
   that no unit names when the write ends is deleted, so no cleanup deletes a
   point a unit names and no refused write leaves a recallable orphan.
B. A reader returns a body and a value of the same write, even when the unit
   was repointed after its row was read.
(C, the description scrub, was replaced: the description is now held out with
the value; see test_reference_description_held.py.)

Fake credentials and example.org hosts only, never real secrets.
"""

from __future__ import annotations

import asyncio
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
    held = ro.split_secret(body).body
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
    body, value, detail = await kc.get_reference_pair(db, unit)
    assert ro.SECRET_PLACEHOLDER in body
    assert value == "password: Pair!Fake9"
    assert detail == "Example portal login"
    assert await kc.get_reference_pair(db, "no-such-unit") is None


# ─── C: the hold-out uses the stored secret and checks its own result ───────


# ─── security review: identifier positions, query secrets, normalisation ────

_LEAK = "Leak!Fake20x"
_IDENTIFIER_POSITION_SHAPES = [
    (f"login: {_LEAK}", _LEAK),
    (f"email: {_LEAK}", _LEAK),
    (f"account: {_LEAK}", _LEAK),
    ("url: https://portal.example.org/?api_key=QueryFake21", "QueryFake21"),
    (f"{_LEAK} / forumuser", _LEAK),  # "secret / login"
]


