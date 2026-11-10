"""Round-1 (PR #4338) fixes for held-out credential values (#2426).

1. A credential upsert is a compare-and-swap against the ingest's own read of
   its key (secret and point, or absent). A same-key writer in another process
   that commits in between makes the write apply nothing; the ingest re-reads
   and runs once more. So a placeholder re-ingest scrubs with the secret the
   commit retains, and the old-point cleanup deletes the real predecessor.
2. A credential whose identifier contains its value is refused: the identifier
   is the entry key, and scrubbing it would file the entry under another key.

Fake credentials and example.org hosts only, never real secrets.
"""

from __future__ import annotations

import asyncio

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.memory import reference_ops as ro
from tests.test_memory.test_reference_secret_class_fixes import (
    _KEY,
    _body,
    _ingest,
    _named_points,
    _PointStore,
)


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


class _PausingStore(_PointStore):
    """Pauses the first store call whose content contains ``marker`` until
    ``resume`` is set; ``entered`` is set when it pauses."""

    def __init__(self, marker: str):
        super().__init__()
        self.marker, self.entered, self.resume = marker, asyncio.Event(), asyncio.Event()

    async def store_reporting_creation(self, content, source, **kwargs):
        if self.marker in content and not self.entered.is_set():
            self.entered.set()
            await self.resume.wait()
        return await super().store_reporting_creation(content, source, **kwargs)


async def _indexed_texts(db, store, unit: str) -> list[str]:
    row = await kc.get(db, unit)
    fts = await db.execute_fetchall(
        "SELECT concept, body, tags FROM knowledge_fts WHERE unit_id = ?",
        (unit,),
    )
    return [row["body"], row["tags"], row["concept"], *fts[0], *store.points.values()]


async def _assert_one_named_point(db, store) -> None:
    named = await _named_points(db)
    assert len(named) == 1
    assert set(store.points) == named, "a recallable point no unit names"


# ─── 1. compare-and-swap ────────────────────────────────────────────────────


async def test_a_placeholder_re_ingest_racing_a_rotation_scrubs_with_the_retained_secret(db):
    """4527738739: A reads Old, pauses in store.store; B rotates the key to New;
    A resumes. A placeholder body whose note repeats New must not index it."""
    store = _PausingStore(marker="note")
    unit = await _ingest(db, store, _body("Example portal login", "password: Old!Fake123"))
    writer_a = asyncio.create_task(
        _ingest(db, store, _body("note New!Fake456", ro.SECRET_PLACEHOLDER)),
    )
    await store.entered.wait()
    await _ingest(db, store, _body("Example portal login", "password: New!Fake456"))
    store.resume.set()
    assert await writer_a == unit

    assert await kc.get_reference_secret(db, unit) == "password: New!Fake456"
    for text in await _indexed_texts(db, store, unit):
        assert "New!Fake456" not in text, text
    await _assert_one_named_point(db, store)


async def test_a_writer_that_read_an_absent_key_never_orphans_the_winners_point(db):
    """4527738743: B reads an absent key and pauses in store.store; A inserts the
    key; B resumes. B must displace A's point, not leave it recallable."""
    store = _PausingStore(marker="from B")
    writer_b = asyncio.create_task(
        _ingest(db, store, _body("Example portal login from B", "password: B!Fake789")),
    )
    await store.entered.wait()
    unit_a = await _ingest(db, store, _body("Example portal login", "password: A!Fake012"))
    store.resume.set()
    assert await writer_b == unit_a  # one key, one unit

    assert await kc.get_reference_secret(db, unit_a) == "password: B!Fake789"
    for text in await _indexed_texts(db, store, unit_a):
        assert "B!Fake789" not in text
    await _assert_one_named_point(db, store)


async def test_a_second_change_raises_to_the_caller_and_leaves_nothing(db, monkeypatch):
    store = _PointStore()
    calls = []

    async def _always_changed(*_a, **_k):
        calls.append(1)
        raise kc.CredentialKeyChanged("changed")

    monkeypatch.setattr(kc, "upsert", _always_changed)
    with pytest.raises(kc.CredentialKeyChanged):
        await _ingest(db, store, _body("Example portal login", "password: Twice!Fake3"))
    assert len(calls) == 2
    assert store.points == {}


async def test_control_a_sequential_rotation_keeps_one_point(db):
    store = _PointStore()
    unit = await _ingest(db, store, _body("Example portal login", "password: One!Fake1"))
    await _ingest(db, store, _body("Example portal login", "password: Two!Fake2"))
    assert await kc.get_reference_secret(db, unit) == "password: Two!Fake2"
    await _assert_one_named_point(db, store)


async def test_a_non_credential_write_passes_no_expectation(db, monkeypatch):
    store = _PointStore()
    seen = []
    real = kc.upsert

    async def _spy(*a, **k):
        seen.append(k.get("expected"))
        return await real(*a, **k)

    monkeypatch.setattr(kc, "upsert", _spy)
    await _ingest(
        db,
        store,
        _body("Example portal", "https://portal.example.org", kind="url"),
        domain="reference.url",
    )
    assert seen == [None]


async def _seed(db, value: str):
    held = _body("Example portal login", ro.SECRET_PLACEHOLDER)
    await kc.upsert(
        db,
        project_type="reference",
        domain="reference.credentials",
        source_doc="x",
        concept=_KEY,
        body=held,
        qdrant_id="p-1",
        secret_value=value,
    )
    return await kc.observe_credential_key(
        db,
        project_type="reference",
        domain="reference.credentials",
        concept=_KEY,
    )


async def _write_expecting(db, expected, value="password: Cas!Fake9"):
    return await kc.upsert(
        db,
        project_type="reference",
        domain="reference.credentials",
        source_doc="cas",
        concept=_KEY,
        body=_body("Example portal login", ro.SECRET_PLACEHOLDER),
        qdrant_id="p-2",
        secret_value=value,
        expected=expected,
    )


async def test_the_upsert_refuses_a_stale_observation_and_writes_nothing(db):
    stale = await _seed(db, "password: First!Fake1")
    await _write_expecting(db, stale, "password: Second!Fake2")  # someone else rotates
    with pytest.raises(kc.CredentialKeyChanged):
        await _write_expecting(db, stale)
    with pytest.raises(kc.CredentialKeyChanged):
        await _write_expecting(db, kc.KeyObservation(None, None, None, None))
    row = await kc.find_by_unique_key(
        db,
        project_type="reference",
        domain="reference.credentials",
        concept=_KEY,
    )
    assert row["source_doc"] == "cas"
    assert await kc.get_reference_secret(db, row["id"]) == "password: Second!Fake2"
    assert (await db.execute_fetchall("SELECT count(*) FROM reference_secrets"))[0][0] == 1


async def test_the_statement_itself_refuses_a_change_after_the_read(db, monkeypatch):
    """The in-statement guard, with the pre-read check blinded: a change that
    lands between the upsert's own read and its statement (another process)."""
    stale = await _seed(db, "password: First!Fake1")
    await _write_expecting(db, stale, "password: Second!Fake2")
    monkeypatch.setattr(kc, "_still_observed", lambda _row, _exp: True)
    with pytest.raises(kc.CredentialKeyChanged):
        await _write_expecting(db, stale)
    with pytest.raises(kc.CredentialKeyChanged):
        await _write_expecting(db, kc.KeyObservation(None, None, None, None))
    assert (await db.execute_fetchall("SELECT count(*) FROM reference_secrets"))[0][0] == 1
    assert len(await db.execute_fetchall("SELECT 1 FROM knowledge_units")) == 1


# ─── 2. an identifier that contains its value ───────────────────────────────


async def test_identifiers_carrying_their_values_never_collide(db):
    """4527738751: identifiers each containing their own value would both read
    "router [redacted]" and the second would overwrite the first. Both are
    refused, and an entry already filed under the redacted key stays intact."""
    store = _PointStore()
    seeded = await _ingest(
        db,
        store,
        _body("Example router", "password: Seed!Fake0"),
        concept="router [redacted]",
    )
    for concept, value in (
        ("router Alpha!Fake1", "Alpha!Fake1"),
        ("router Beta!Fake2", "Beta!Fake2"),
    ):
        with pytest.raises(ValueError, match="identifier must not contain its value") as refused:
            await _ingest(db, store, _body("Example router", f"password: {value}"), concept=concept)
        assert value not in str(refused.value)
    assert len(await db.execute_fetchall("SELECT 1 FROM knowledge_units")) == 1
    assert await kc.get_reference_secret(db, seeded) == "password: Seed!Fake0"
    await _assert_one_named_point(db, store)


async def test_a_credential_without_an_identifier_is_refused(db):
    """With no concept the key would be derived from the SCRUBBED body, so two
    credentials differing only in their values would share it (security review
    of 4527738751's fix). Such a write is refused before any store write."""
    store = _PointStore()
    for value in ("Alpha!Fake1", "Beta!Fake2"):
        with pytest.raises(ValueError, match="needs an explicit identifier") as refused:
            await _ingest(db, store, _body("router", f"password: {value}"), concept=None)
        assert value not in str(refused.value)
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_units") == []
    assert store.points == {}


async def test_a_placeholder_re_ingest_whose_identifier_carries_the_stored_value_is_refused(db):
    """A key written before this rule (directly, so its identifier carries the
    value): a placeholder re-ingest would scrub that identifier into another
    key, so it is refused, and the old entry keeps its secret."""
    legacy = "router Kept!Fake5"
    unit, _ = await kc.upsert(
        db, project_type="reference", domain="reference.credentials", source_doc="x",
        concept=legacy, body=_body("Example router", ro.SECRET_PLACEHOLDER),
        qdrant_id="p-legacy", secret_value="password: Kept!Fake5",
    )
    with pytest.raises(ValueError, match="identifier must not contain its value"):
        await _ingest(
            db, _PointStore(), _body("Example router", ro.SECRET_PLACEHOLDER), concept=legacy,
        )
    assert await kc.get_reference_secret(db, unit) == "password: Kept!Fake5"
