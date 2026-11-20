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
import contextlib
import json
import re

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.memory import knowledge_ingest as ki
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
    """Pauses the first ingest made while ``armed`` just before it takes the
    knowledge writer lock, after its read of the key and before its store and
    commit; ``entered`` is set when it pauses. The lock serializes the
    store-to-cleanup window (#4161), so the race the compare-and-swap guards is
    the one between the read and the lock. (Bodies of one key are alike: the
    description is held out, so the call is picked by order, not content.)"""

    def __init__(self, monkeypatch, armed: bool = True):
        super().__init__()
        self.armed, self.entered, self.resume = armed, asyncio.Event(), asyncio.Event()
        real = ki.writer

        @contextlib.asynccontextmanager
        async def _writer(*args, **kwargs):
            if self.armed and not self.entered.is_set():
                self.entered.set()
                await self.resume.wait()
            async with real(*args, **kwargs):
                yield

        monkeypatch.setattr(ki, "writer", _writer)


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


async def test_a_placeholder_re_ingest_racing_a_rotation_pairs_with_the_retained_secret(
    db, monkeypatch,
):
    """4527738739: A reads Old, pauses before its store; B rotates the key to New;
    A resumes. A placeholder body whose note repeats New must not index it."""
    store = _PausingStore(monkeypatch, armed=False)
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


async def test_a_writer_that_read_an_absent_key_never_orphans_the_winners_point(
    db, monkeypatch,
):
    """4527738743: B reads an absent key and pauses before its store; A inserts the
    key; B resumes. B must displace A's point, not leave it recallable."""
    store = _PausingStore(monkeypatch)
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
    # The first try and five fresh reads (owner ruling: retry until it lands).
    assert len(calls) == 6
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
    """4527738751: identifiers each containing their own value both read
    "router [held]" once masked. Identity is the full name (#2426), so the two
    are two entries, and an entry filed under the bare masked text stays
    intact; no key spells a value."""
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
    assert len({seeded, *units}) == 3
    assert await kc.get_reference_secret(db, seeded) == "password: Seed!Fake0"
    assert await kc.get_reference_secret(db, units[1]) == "password: Beta!Fake2"
    for unit in units:
        row = await kc.get(db, unit)
        assert "Fake" not in row["concept"] and ro.display_name(row) == "router [held]"


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
    assert len(concepts) == 2 and all(re.fullmatch(r"cred-[0-9a-f]{12}", c) for c in concepts)
    assert await kc.get_reference_secret(db, units[1]) == "password: Beta!Fake2"


@pytest.mark.xfail(strict=True, reason="fixed by the credential move PR")
async def test_a_placeholder_re_ingest_whose_identifier_carries_the_stored_value_moves_to_a_masked_key(db):
    """A key written before this rule (directly, so its identifier carries the
    value): a placeholder re-ingest moves that SAME row to the masked
    identifier (audit 2: an untagged entry whose key spells a current secret is
    re-keyed, never left beside a second entry), its stored value kept."""
    legacy = "router Kept!Fake5"
    unit, _ = await kc.upsert(
        db, project_type="reference", domain="reference.credentials", source_doc="x",
        concept=legacy, body=_body("Example router", ro.SECRET_PLACEHOLDER),
        qdrant_id="p-legacy", secret_value="password: Kept!Fake5",
    )
    moved = await _ingest(
        db, _PointStore(), _body("Example router", ro.SECRET_PLACEHOLDER), concept=legacy,
    )
    assert moved == unit and re.fullmatch(r"cred-[0-9a-f]{12}", (await kc.get(db, moved))["concept"])
    assert ro.display_name(await kc.get(db, moved)) == "router [held]"
    assert await kc.get_reference_secret(db, moved) == "password: Kept!Fake5"
    assert len(await db.execute_fetchall("SELECT 1 FROM knowledge_units")) == 1


# ─── 3. same-name writes racing (round-2 class audit B) ──────────────────────
#
# A write builds its masks, its target and its sameness from the entries its
# name resolves to, read before it holds the writer lock; the compare-and-swap
# covers only its own key. Under the lock it reads them again, and a change
# has it build the write again there, never an error: every writer lands,
# masked against the others.


def _pause_at(monkeypatch, at: str):
    """A gate that pauses the first write to reach ``at`` ("lock": the writer
    lock's entry; "resolve": just after its read of the name's entries) until
    ``gate.resume`` is set; ``gate.entered`` is set when it pauses."""
    from types import SimpleNamespace

    gate = SimpleNamespace(armed=True, entered=asyncio.Event(), resume=asyncio.Event())

    async def _hold():
        if gate.armed and not gate.entered.is_set():
            gate.entered.set()
            await gate.resume.wait()

    if at == "lock":
        real_writer = ki.writer

        @contextlib.asynccontextmanager
        async def _writer(*args, **kwargs):
            await _hold()
            async with real_writer(*args, **kwargs):
                yield

        monkeypatch.setattr(ki, "writer", _writer)
    else:
        real_resolve = ki._resolve_by_name

        async def _resolve(*args, **kwargs):
            out = await real_resolve(*args, **kwargs)
            await _hold()
            return out

        monkeypatch.setattr(ki, "_resolve_by_name", _resolve)
    return gate


async def _race(gate, first, second):
    """``first()`` paused at its gate while ``second()`` runs to completion,
    then resumed: (first's result, second's result)."""
    task = asyncio.create_task(first())
    await asyncio.wait_for(gate.entered.wait(), 10)
    gate.armed = False
    other = await second()
    gate.resume.set()
    return await asyncio.wait_for(task, 10), other


async def _all_indexed(db, store) -> list[str]:
    from tests.test_memory.test_reference_opaque_identity import _indexed

    return await _indexed(db, store)


_RACE_NAME = "Example portal Fake9pwx"
_RACE_TAGS = json.dumps(["reference", "credentials", "tag Fake9pwx"])


@pytest.mark.parametrize("at", ["lock", "resolve"])
async def test_b1_an_upload_racing_a_trusted_save_of_its_name_is_masked(db, monkeypatch, at):
    """The upload read the name's entries before the trusted save committed:
    its name and tags must still mask the saved value."""
    from tests.test_memory.test_reference_opaque_identity import _held_save, _untrusted

    store = _PointStore()
    gate = _pause_at(monkeypatch, at)
    upload, saved = await _race(
        gate,
        lambda: _untrusted(db, store, _RACE_NAME, "uploaded notes: Up9load!Fake1", tags_json=_RACE_TAGS),
        lambda: _held_save(db, store, _RACE_NAME, "Fake9pwx"),
    )
    assert upload != saved and "Fake9pwx" not in (upload.name or "")
    for text in await _all_indexed(db, store):
        assert "Fake9pwx" not in text, text


@pytest.mark.parametrize("at", ["lock", "resolve"])
async def test_b10_a_trusted_save_racing_an_upload_of_its_name_is_masked(db, monkeypatch, at):
    """The reverse order: the trusted save read the name's entries before an
    upload whose value its name and tags spell committed."""
    from tests.test_memory.test_reference_opaque_identity import _held_save, _untrusted

    store = _PointStore()
    gate = _pause_at(monkeypatch, at)
    saved, upload = await _race(
        gate,
        lambda: _held_save(db, store, _RACE_NAME, "Tr1st!Fake2", tags_json=_RACE_TAGS),
        lambda: _untrusted(db, store, _RACE_NAME, "Fake9pwx"),
    )
    assert upload != saved and "Fake9pwx" not in (saved.name or "")
    for text in await _all_indexed(db, store):
        assert "Fake9pwx" not in text, text


@pytest.mark.parametrize("at", ["lock", "resolve"])
async def test_b5_two_identical_concurrent_uploads_are_one_entry(db, monkeypatch, at):
    from tests.test_memory.test_reference_opaque_identity import _untrusted

    store = _PointStore()
    gate = _pause_at(monkeypatch, at)
    first, second = await _race(
        gate,
        lambda: _untrusted(db, store, "Example doc", "same Fake9pwx content"),
        lambda: _untrusted(db, store, "Example doc", "same Fake9pwx content"),
    )
    assert first == second
    assert len(await db.execute_fetchall("SELECT 1 FROM knowledge_units")) == 1
    await _assert_one_named_point(db, store)


async def test_b3_a_rotation_between_the_read_and_the_lock_rereads_one_snapshot(db, monkeypatch):
    """The target rotated after the write read the name's entries: the write
    builds again under the lock from a fresh read (one snapshot feeds the
    target, the masks and the recorded spans) and lands as the last write,
    masking the rotated value."""
    from tests.test_memory.test_reference_opaque_identity import _held_save

    name = "Example portal Rot2!Fake4"
    store = _PointStore()
    unit = await _held_save(db, store, name, "Old1!Fake0")
    attempts = []
    real_once = ki._ingest_once

    async def _counted(**kwargs):
        attempts.append(1)
        return await real_once(**kwargs)

    monkeypatch.setattr(ki, "_ingest_once", _counted)
    gate = _pause_at(monkeypatch, "resolve")
    first, second = await _race(
        gate,
        lambda: _held_save(db, store, name, "Rot1!Fake3"),
        lambda: _held_save(db, store, name, "Rot2!Fake4"),
    )
    assert first == second == unit
    assert await kc.get_reference_secret(db, unit) == "Rot1!Fake3"
    # The paused write and the save in its window: the paused write builds
    # again under the writer lock, never from a fresh ingest attempt.
    assert len(attempts) == 2
    ref = (await kc.get(db, unit))["secret_ref"]
    rows = await db.execute_fetchall("SELECT meta FROM reference_secrets WHERE id = ?", (ref,))
    meta = json.loads(rows[0][0])
    assert "Rot2!Fake4" in meta[kc.IDENTITY_META]["masked_spans"]
    for text in await _all_indexed(db, store):
        assert "Rot2!Fake4" not in text, text
    await _assert_one_named_point(db, store)


@pytest.mark.parametrize("trusted_first", [True, False])
async def test_h1_twenty_four_concurrent_uploads_of_one_name_all_land(db, trusted_first):
    """Round-2 fix audit H1: distinct uploads of one name, all at once. Each
    commit changes the entries every other write resolved, and each is built
    again under the lock rather than retried, so none errors (the reread loop
    raised for 18 of 24), every one lands, and no indexed text carries a held
    value: the trusted save's or another upload's."""
    from tests.test_memory.test_reference_opaque_identity import _held_save, _untrusted

    store = _PointStore()
    if trusted_first:
        await _held_save(db, store, _RACE_NAME, "Fake9pwx")
    count = 24

    async def _one(i):
        tags = json.dumps(["reference", "credentials", "tag Fake9pwx", f"tag Up{i}Fake"])
        return await _untrusted(db, store, _RACE_NAME, f"upload body {i} Up{i}Fake", tags_json=tags)

    results = await asyncio.gather(*(_one(i) for i in range(count)), return_exceptions=True)
    assert not [r for r in results if isinstance(r, BaseException)], results
    assert len(set(results)) == count
    units = await db.execute_fetchall("SELECT 1 FROM knowledge_units")
    assert len(units) == count + trusted_first
    held = [f"Up{i}Fake" for i in range(count)] + (["Fake9pwx"] if trusted_first else [])
    for text in await _all_indexed(db, store):
        for value in held:
            if value != "Fake9pwx" or trusted_first:
                assert not re.search(rf"(?<![0-9]){value}", text), (value, text)
