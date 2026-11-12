"""Round-1 (PR #4338) fixes for held-out credential values (#2426).

1. A credential upsert is a compare-and-swap against the ingest's own read of
   its key (secret and point, or absent). A same-key writer in another process
   that commits in between makes the write apply nothing; the ingest re-reads
   and runs once more. So a placeholder re-ingest pairs its description with
   the secret the commit retains, and the old-point cleanup deletes the real
   predecessor.
2. A credential whose identifier contains its value is refused: the identifier
   is the entry key, so it is refused, never filed under another key.

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
    """Pauses the first store call made while ``armed`` until ``resume`` is
    set; ``entered`` is set when it pauses. (Bodies of one key are now alike:
    the description is held out, so the call is picked by order, not content.)"""

    def __init__(self, armed: bool = True):
        super().__init__()
        self.armed, self.entered, self.resume = armed, asyncio.Event(), asyncio.Event()

    async def store_reporting_creation(self, content, source, **kwargs):
        if self.armed and not self.entered.is_set():
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


async def test_a_placeholder_re_ingest_racing_a_rotation_pairs_with_the_retained_secret(db):
    """4527738739: A reads Old, pauses in store.store; B rotates the key to New;
    A resumes. A placeholder body whose note repeats New must not index it."""
    store = _PausingStore(armed=False)
    unit = await _ingest(db, store, _body("Example portal login", "password: Old!Fake123"))
    store.armed = True
    writer_a = asyncio.create_task(
        _ingest(db, store, _body("note New!Fake456", ro.SECRET_PLACEHOLDER)),
    )
    await store.entered.wait()
    await _ingest(db, store, _body("Example portal login", "password: New!Fake456"))
    store.resume.set()
    assert await writer_a == unit

    assert await kc.get_reference_secret(db, unit) == "password: New!Fake456"
    # The new description of A, paired with the value the commit retains.
    assert await ro.reveal_reference(db, await kc.get(db, unit)) == (
        "password: New!Fake456", "note New!Fake456",
    )
    for text in await _indexed_texts(db, store, unit):
        assert "New!Fake456" not in text, text
    await _assert_one_named_point(db, store)


async def test_a_writer_that_read_an_absent_key_never_orphans_the_winners_point(db):
    """4527738743: B reads an absent key and pauses in store.store; A inserts the
    key; B resumes. B must displace A's point, not leave it recallable."""
    store = _PausingStore()
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
    """4527738751, reworked by the owner ruling on #2426 PR A: identifiers each
    containing their own value both read "router [held]". That masked text is
    the entry's identity, so the second (a rotation) updates the first under
    its tagged key, and an entry filed under the bare masked text stays intact."""
    store = _PointStore()
    seeded = await _ingest(
        db,
        store,
        _body("Example router", "password: Seed!Fake0"),
        concept="router [held]",
    )
    units = [
        await _ingest(db, store, _body("Example router", f"password: {value}"), concept=f"router {value}")
        for value in ("Alpha!Fake1", "Beta!Fake2")
    ]
    assert units[0] == units[1] and units[0] != seeded
    assert len(await db.execute_fetchall("SELECT 1 FROM knowledge_units")) == 2
    assert await kc.get_reference_secret(db, seeded) == "password: Seed!Fake0"
    assert await kc.get_reference_secret(db, units[1]) == "password: Beta!Fake2"
    assert "Fake" not in (await kc.get(db, units[1]))["concept"]


async def test_a_credential_without_an_identifier_gets_a_minted_key(db):
    """With no concept the key would be derived from the HELD body, so two
    credentials differing only in their values would share it (security review
    of 4527738751's fix). Each gets a minted key of its own (#2426 PR A)."""
    store = _PointStore()
    units = [
        await _ingest(db, store, _body("router", f"password: {value}"), concept=None)
        for value in ("Alpha!Fake1", "Beta!Fake2")
    ]
    concepts = {(await kc.get(db, u))["concept"] for u in units}
    assert len(concepts) == 2 and all(c.startswith("credential #") for c in concepts)
    assert await kc.get_reference_secret(db, units[1]) == "password: Beta!Fake2"


async def test_a_placeholder_re_ingest_whose_identifier_carries_the_stored_value_moves_to_a_masked_key(db):
    """A key written before this rule (directly, so its identifier carries the
    value): a placeholder re-ingest is stored under the masked identifier with
    the stored value carried over, and the old entry keeps its secret."""
    legacy = "router Kept!Fake5"
    unit, _ = await kc.upsert(
        db, project_type="reference", domain="reference.credentials", source_doc="x",
        concept=legacy, body=_body("Example router", ro.SECRET_PLACEHOLDER),
        qdrant_id="p-legacy", secret_value="password: Kept!Fake5",
    )
    moved = await _ingest(
        db, _PointStore(), _body("Example router", ro.SECRET_PLACEHOLDER), concept=legacy,
    )
    assert moved != unit and (await kc.get(db, moved))["concept"].startswith("router [held] #")
    assert await kc.get_reference_secret(db, moved) == "password: Kept!Fake5"
    assert await kc.get_reference_secret(db, unit) == "password: Kept!Fake5"
