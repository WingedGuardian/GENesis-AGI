"""A credential reference's value is held out of every indexed store (#2426).

The value used to sit in the body, and the body is what knowledge_units,
knowledge_fts, memory_fts, the Qdrant payload and the embedding all index, so
every general recall surface could return it. Now ``ingest_knowledge_unit``, the
chokepoint every writer passes through, replaces it with a placeholder (and
redacts fragments of it wherever else the body repeats them), and the value goes
to ``reference_secrets``, written in the unit's own transaction. Only
``reference_ops.resolve_value`` reads it back.

Fake credentials and TEST-NET (203.0.113.x) values only — never real secrets.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.mcp.memory.knowledge import _format_reference_body as fmt_mcp
from genesis.memory import reference_ops as ro
from genesis.memory.knowledge_ingest import ingest_knowledge_unit
from genesis.memory.reference_extraction import (
    _format_reference_body as fmt_extraction,
)

_PASSWORD = "Hunter2!xyz-fake"
_VALUE = f"user: example-admin, password: {_PASSWORD}"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _store() -> MagicMock:
    store = MagicMock()
    store.store = AsyncMock(return_value="qid-1")
    store.delete = AsyncMock(return_value={})
    store._embeddings.model_name = "test-model"
    return store


def _body(value: str = _VALUE, description: str = "Router admin panel login", **kw) -> str:
    return fmt_mcp(
        kind="credentials",
        identifier="Lab router admin",
        description=description,
        value=value,
        tags=kw.get("tags"),
        source=kw.get("source"),
    )


async def _ingest(db, store, body: str, *, domain: str = "reference.credentials", tags=None) -> str:
    return await ingest_knowledge_unit(
        store=store,
        db=db,
        content=body,
        project=ro.REFERENCE_PROJECT,
        domain=domain,
        concept="Lab router admin",
        tags_json=json.dumps(tags) if tags else None,
        collection="episodic_memory",
        memory_type="episodic",
    )


async def _secret(db, unit_id: str) -> str | None:
    """The value the unit names through its secret_ref."""
    return await kc.get_reference_secret(db, unit_id)


async def _secret_rows(db) -> int:
    return (await db.execute_fetchall("SELECT count(*) FROM reference_secrets"))[0][0]


# ─── what every indexed store receives ───────────────────────────────────────


async def test_no_indexed_store_receives_the_value(db):
    store = _store()
    unit_id = await _ingest(db, store, _body(description=f"Router login, password is {_PASSWORD}"))

    indexed = store.store.await_args.args[0]
    row = await kc.get(db, unit_id)
    fts = await db.execute_fetchall("SELECT body FROM knowledge_fts WHERE unit_id = ?", (unit_id,))
    for text in (indexed, row["body"], fts[0][0]):
        assert _PASSWORD not in text, text
        assert f"Value: {ro.SECRET_PLACEHOLDER}" in text
        # The description is held out with the value, whatever it says.
        assert "Router login" not in text
        assert f"\n\n{ro.DETAIL_PLACEHOLDER}\n\n" in text
    assert await _secret(db, unit_id) == _VALUE


async def test_both_writers_bodies_are_held_out(db):
    """The contract holds for each formatter that writes a credential body."""
    for i, body in enumerate(
        (
            _body(),
            fmt_extraction(
                kind="credentials",
                identifier="Lab router admin",
                description="Router admin panel login",
                value=_VALUE,
                tags=["router"],
                session_id="s-1",
            ),
        )
    ):
        store = _store()
        unit_id = await ingest_knowledge_unit(
            store=store,
            db=db,
            content=body,
            project=ro.REFERENCE_PROJECT,
            domain="reference.credentials",
            concept=f"writer-{i}",
        )
        assert _PASSWORD not in store.store.await_args.args[0]
        assert await _secret(db, unit_id) == _VALUE


async def test_an_identifier_kind_keeps_its_value_in_the_body(db):
    """A URL or an IP is what its lookup searches for: not held out."""
    store = _store()
    body = fmt_mcp(
        kind="network",
        identifier="Lab router",
        description="Router on the lab net",
        value="203.0.113.10",
        tags=None,
        source=None,
    )
    unit_id = await _ingest(db, store, body, domain="reference.network")
    assert "Value: 203.0.113.10" in store.store.await_args.args[0]
    assert await _secret(db, unit_id) is None


# ─── which fragments are secret ──────────────────────────────────────────────


# ─── refusals and re-ingest ──────────────────────────────────────────────────


async def test_a_credential_without_a_value_line_holds_its_whole_remainder_out(db):
    """No 'Value:' line: nothing tells the value from the description, so the
    whole text after the header is held out as the value (#2426 PR A)."""
    store = _store()
    unit_id = await _ingest(
        db, store, f"[reference.credentials] Lab router admin\n\npassword {_PASSWORD}"
    )
    assert _PASSWORD not in store.store.await_args.args[0]
    assert _PASSWORD not in (await kc.get(db, unit_id))["body"]
    assert await _secret(db, unit_id) == f"password {_PASSWORD}"


async def test_a_card_number_is_refused(db):
    store = _store()
    with pytest.raises(ValueError, match="card"):
        await _ingest(db, store, _body(value="4111 1111 1111 1111"))
    store.store.assert_not_awaited()


async def test_a_new_value_replaces_the_stored_one(db):
    unit_id = await _ingest(db, _store(), _body())
    again = await _ingest(
        db, _store(), _body(value="user: example-admin, password: Rotated!Fake-2")
    )
    assert again == unit_id
    assert await _secret(db, unit_id) == "user: example-admin, password: Rotated!Fake-2"


async def test_re_ingesting_a_held_out_body_keeps_its_secret(db):
    """A body already carrying the placeholder brings no new value."""
    unit_id = await _ingest(db, _store(), _body())
    held = (await kc.get(db, unit_id))["body"]
    await _ingest(db, _store(), held)
    assert await _secret(db, unit_id) == _VALUE


async def test_a_failed_secret_write_leaves_nothing_for_another_commit(db):
    """No reference_secrets table (a process ahead of its schema init): the
    secret is written FIRST, so the unit's own writes never start, and a later
    commit by another writer on the shared connection carries none of them."""
    await db.execute("DROP TABLE reference_secrets")
    with pytest.raises(aiosqlite.OperationalError):
        await _ingest(db, _store(), _body())
    await db.commit()
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_units") == []
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_fts") == []


# ─── readers ─────────────────────────────────────────────────────────────────


async def test_resolve_value_reads_the_secret_and_parses_everything_else(db):
    unit_id = await _ingest(db, _store(), _body())
    assert await ro.resolve_value(db, await kc.get(db, unit_id)) == _VALUE
    legacy = {
        "id": "legacy",
        "project_type": "reference",
        "domain": "reference.credentials",
        "body": _body(),
    }
    assert await ro.resolve_value(db, legacy) == _VALUE


async def test_a_missing_secret_reads_as_missing_never_as_the_placeholder(db, caplog):
    unit_id = await _ingest(db, _store(), _body())
    await db.execute("DELETE FROM reference_secrets")
    row = await kc.get(db, unit_id)
    assert await ro.resolve_value(db, row) is None
    body, missing = await ro.compose_body(db, row)
    assert missing is True
    assert "no secret is stored" in caplog.text


async def test_compose_body_puts_the_value_back_on_its_line(db):
    unit_id = await _ingest(db, _store(), _body())
    body, missing = await ro.compose_body(db, await kc.get(db, unit_id))
    assert missing is False
    assert f"Value: {_VALUE}" in body
    assert ro.parse_reference_body(body)["value"] == _VALUE


async def test_delete_takes_the_secret_with_the_unit(db):
    unit_id = await _ingest(db, _store(), _body())
    assert await _secret_rows(db) == 1
    assert await kc.delete(db, unit_id) is True
    assert await _secret_rows(db) == 0


async def test_no_recall_row_carries_the_value(db):
    """knowledge_units is read with SELECT * into recall rows: the value lives
    in another table, so no column of the row holds it."""
    unit_id = await _ingest(db, _store(), _body())
    row = await kc.get(db, unit_id)
    assert not any(_PASSWORD in str(v) for v in row.values())


# ─── review round fixes ──────────────────────────────────────────────────────


async def test_a_raw_delete_still_takes_the_secret(db):
    """A process on pre-#2426 code, or raw SQL, deletes only the unit: the
    trigger retires the secret anyway."""
    unit_id = await _ingest(db, _store(), _body())
    assert await _secret_rows(db) == 1
    await db.execute("DELETE FROM knowledge_units WHERE id = ?", (unit_id,))
    assert await _secret_rows(db) == 0


async def test_the_db_layer_refuses_a_credential_body_that_carries_its_value(db):
    """A writer that bypasses ingest_knowledge_unit cannot index the value."""
    for domain in ("reference.credentials", "Reference.Credentials"):
        with pytest.raises(ValueError, match="carries its value"):
            await kc.upsert(
                db,
                project_type="reference",
                domain=domain,
                source_doc="x",
                concept="c",
                body=_body(),
            )
        with pytest.raises(ValueError, match="carries its value"):
            await kc.insert(
                db,
                project_type="reference",
                domain=domain,
                source_doc="x",
                concept="c",
                body=_body(),
            )


async def test_an_odd_domain_spelling_is_held_out_not_refused(db):
    unit_id = await _ingest(db, _store(), _body(), domain="Reference.Credentials")
    assert await _secret(db, unit_id) == _VALUE


# ─── review round 1 (#3898) ──────────────────────────────────────────────────


async def test_an_identifier_carrying_the_value_is_masked_under_a_tagged_key(db):
    """An extraction with no entities derives the identifier from its own text,
    which can carry the password. The identifier is stored with the value masked
    and a random tag, so two such entries never share a key (#2426 PR A)."""
    store = _store()
    unit_id = await ingest_knowledge_unit(
        store=store,
        db=db,
        content=_body(),
        project=ro.REFERENCE_PROJECT,
        domain="reference.credentials",
        concept=f"router login {_PASSWORD}",
    )
    row = await kc.get(db, unit_id)
    assert row["concept"].startswith("router login [held] #")
    assert unit_id.concept == row["concept"]
    assert _PASSWORD not in store.store.await_args.args[0]
    assert await _secret(db, unit_id) == _VALUE


async def test_the_db_layer_refuses_a_second_value_line_after_the_placeholder(db):
    body = f"[reference.credentials] X\n\nd\n\nValue: {kc.SECRET_PLACEHOLDER}\n\nValue: {_PASSWORD}"
    with pytest.raises(ValueError, match="carries its value"):
        await kc.upsert(
            db,
            project_type="reference",
            domain="reference.credentials",
            source_doc="x",
            concept="c",
            body=body,
        )
    body = f"[reference.credentials] X\n\nd\n\nValue: {kc.SECRET_PLACEHOLDER}\n{_PASSWORD}"
    with pytest.raises(ValueError, match="carries its value"):
        await kc.upsert(
            db,
            project_type="reference",
            domain="reference.credentials",
            source_doc="x",
            concept="c",
            body=body,
        )


def test_a_description_carrying_the_value_marker_holds_out_more():
    """Both this split and parse_reference_body cut at the FIRST marker: the
    rest of the body, the description's tail included, is held as the value."""
    held = ro.split_secret(_body(description="note\n\nValue: tail-fake"))
    assert "tail-fake" not in held.body and _PASSWORD not in held.body
    assert held.value == f"tail-fake\n\nValue: {_VALUE}" and held.detail == "note"


async def test_a_new_credential_with_no_value_to_hold_is_stored_held_empty(db):
    """A held-out body under a NEW identifier brings no secret: stored with an
    empty held value, never refused (#2426 PR A, owner ruling c91c5b74)."""
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")
    unit = await ingest_knowledge_unit(
        store=_store(),
        db=db,
        content=held,
        project=ro.REFERENCE_PROJECT,
        domain="reference.credentials",
        concept="brand new",
    )
    assert await _secret(db, unit) == ""


async def test_the_secret_lands_on_the_row_that_survives_the_key_conflict(db):
    """A caller passing a fresh id for an existing key: the row keeps its
    original id, and the secret must be written under THAT id."""
    first = await _ingest(db, _store(), _body())
    actual, inserted = await kc.upsert(
        db,
        id="some-other-id",
        project_type="reference",
        domain="reference.credentials",
        source_doc="x",
        concept="Lab router admin",
        body=(await kc.get(db, first))["body"],
        secret_value="user: example-admin, password: New!Fake-3",
    )
    assert actual == first and inserted is False
    assert await _secret(db, first) == "user: example-admin, password: New!Fake-3"
    assert await _secret(db, "some-other-id") is None


async def test_a_refused_write_leaves_nothing_for_another_commit(db):
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")
    with pytest.raises(ValueError):
        await kc.upsert(
            db,
            project_type="reference",
            domain="reference.credentials",
            source_doc="x",
            concept="new",
            body=held,
            _commit=False,
        )
    await db.commit()
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_units") == []
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_fts") == []


async def test_a_point_another_unit_still_names_is_not_deleted(db):
    store = _store()
    a = await ingest_knowledge_unit(
        store=store, db=db, content="shared text", project="p", domain="d", concept="a"
    )
    await ingest_knowledge_unit(
        store=store, db=db, content="shared text", project="p", domain="d", concept="b"
    )
    store.store = AsyncMock(return_value="qid-2")
    await ingest_knowledge_unit(
        store=store, db=db, content="changed", project="p", domain="d", concept="a"
    )
    store.delete.assert_not_awaited()
    assert (await kc.get(db, a))["qdrant_id"] == "qid-2"


# ── Round 2: every captured secret, every reference kind, checks before any store ──


async def test_a_credential_with_no_value_to_hold_stores_its_vector_with_its_row(db):
    store = _store()
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")
    unit = await ingest_knowledge_unit(
        store=store,
        db=db,
        content=held,
        project=ro.REFERENCE_PROJECT,
        domain="reference.credentials",
        concept="brand new",
    )
    assert (await kc.get(db, unit))["qdrant_id"] == "qid-1"


def test_the_project_is_case_folded_like_the_domain():
    assert kc.holds_secret("Reference", "reference.credentials")
    assert kc.holds_secret(" reference ", "Reference.Credentials")
    assert not kc.holds_secret("references", "reference.credentials")


async def test_a_reference_project_in_another_case_is_stored_canonically(db):
    unit = await ingest_knowledge_unit(
        store=_store(),
        db=db,
        content=_body(),
        project="Reference",
        domain="reference.credentials",
        concept="Lab router admin",
    )
    row = await kc.get(db, unit)
    assert row["project_type"] == "reference"
    assert _PASSWORD not in row["body"]


# ─── #3909 review: the hold-out half (split PR 1) ─────────────────────────────


@pytest.fixture
def memory_mcp_store(monkeypatch):
    """The memory MCP module's store and db, as the orchestrator and the upload
    path reach them, with a store whose ``store`` call is observable."""
    import genesis.mcp.memory_mcp as memory_mod

    store = _store()
    monkeypatch.setattr(memory_mod, "_require_init", lambda: None)
    monkeypatch.setattr(memory_mod, "_store", store)
    monkeypatch.setattr(memory_mod, "_db", MagicMock())
    return store


@pytest.mark.parametrize("domain", ["reference.credentials", "Reference.Credentials"])
async def test_distillation_holds_a_credential_unit_out_before_any_vector(
    memory_mcp_store,
    tmp_path,
    domain,
    db,
    monkeypatch,
):
    """4476130424: behaviour, not source order. The unit goes through the
    hold-out chokepoint, never refused (#2426 PR A)."""
    import genesis.mcp.memory_mcp as memory_mod
    from genesis.knowledge.distillation import KnowledgeUnit
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator

    monkeypatch.setattr(memory_mod, "_db", db)
    orch = KnowledgeOrchestrator(
        registry=MagicMock(),
        distillation=MagicMock(),
        manifest=ManifestManager(root=tmp_path / "knowledge"),
    )
    unit = KnowledgeUnit(concept="Lab router", body=f"password {_PASSWORD}", domain=domain)
    ids = await orch._store_units(
        [unit],
        project_type="reference",
        source="notes.md",
        content=MagicMock(),
        purpose=None,
    )
    assert _PASSWORD not in memory_mcp_store.store.await_args.args[0]
    assert await _secret(db, ids[0]) == f"password {_PASSWORD}"


@pytest.mark.parametrize("domain", ["reference.credentials", "Reference.Credentials"])
async def test_an_upload_to_a_credential_domain_is_held_out_before_any_vector(
    memory_mcp_store,
    tmp_path,
    domain,
    db,
    monkeypatch,
):
    import genesis.mcp.memory_mcp as memory_mod
    from genesis.knowledge.ingest_upload import _store_as_is

    monkeypatch.setattr(memory_mod, "_db", db)
    upload = tmp_path / "creds.txt"
    upload.write_text(f"router password {_PASSWORD}")
    ids = await _store_as_is(
        str(upload),
        "creds.txt",
        project_type="reference",
        domain=domain,
        purpose=None,
        context="",
    )
    assert _PASSWORD not in memory_mcp_store.store.await_args.args[0]
    assert await _secret(db, ids[0]) == f"router password {_PASSWORD}"


async def _credential_units_without_a_secret(db) -> list:
    return await db.execute_fetchall(
        "SELECT id FROM knowledge_units WHERE domain = 'reference.credentials' "
        "AND (secret_ref IS NULL OR secret_ref NOT IN (SELECT id FROM reference_secrets))"
    )


async def test_the_database_refuses_a_raw_credential_body_that_carries_its_value(db):
    """b13ee875: a writer that runs no current code (raw SQL here) is refused by
    the knowledge_units triggers, on insert and on an update of the body."""
    insert = (
        "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
        "ingested_at) VALUES (?, ?, ?, 'x', ?, ?, 'now')"
    )
    for domain in ("reference.credentials", "Reference.Credentials "):
        with pytest.raises(aiosqlite.IntegrityError, match="carries its value"):
            await db.execute(insert, ("raw", "reference", domain, "c", _body()))
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")
    # (Text on a later line after the placeholder is beyond what the SQL
    # checks; refuse_inline_secret refuses that one.)
    for bad in (
        held.replace("Value: ", "Value:  "),  # odd spacing
        held + _PASSWORD,  # text glued to the placeholder
        held + f"\n\nValue: {_PASSWORD}",  # a second value line
    ):
        with pytest.raises(aiosqlite.IntegrityError, match="carries its value"):
            await db.execute(insert, ("raw", "reference", "reference.credentials", "c", bad))
    await db.execute(
        "INSERT INTO reference_secrets (id, value, created_at) VALUES ('s-ok', 'fake-1', 'now')"
    )
    await db.execute(
        "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
        "ingested_at, secret_ref) VALUES ('ok', 'reference', 'reference.credentials', 'x', "
        "'c', ?, 'now', 's-ok')",
        (held,),
    )
    with pytest.raises(aiosqlite.IntegrityError, match="carries its value"):
        await db.execute("UPDATE knowledge_units SET body = ? WHERE id = 'ok'", (_body(),))
    # Another kind keeps its value in the body, and the database lets it.
    await db.execute(insert, ("url", "reference", "reference.url", "u", _body()))


async def test_a_row_written_before_the_change_still_updates_its_counters(db):
    """The UPDATE triggers watch body, project_type, domain and secret_ref only:
    a legacy credential row (value still inline, no secret_ref) keeps its
    retrieval counter working."""
    # A row as a writer from before the change left it.
    await db.execute("DROP TRIGGER knowledge_units_credential_body_insert")
    await db.execute("DROP TRIGGER knowledge_units_secret_ref_insert")
    await db.execute(
        "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
        "ingested_at) VALUES ('legacy', 'reference', 'reference.credentials', 'x', 'c', ?, 'now')",
        (_body(),),
    )
    await db.execute(
        "UPDATE knowledge_units SET retrieved_count = retrieved_count + 1 WHERE id = 'legacy'"
    )
    row = await kc.get(db, "legacy")
    assert row["retrieved_count"] == 1


async def test_a_credential_domain_in_another_case_is_stored_canonically(db):
    """4476130343: the lookup's audit check compares the domain exactly."""
    unit = await _ingest(db, _store(), _body(), domain=" Reference.Credentials")
    assert (await kc.get(db, unit))["domain"] == "reference.credentials"


async def test_deleting_a_reference_keeps_a_point_another_unit_names(db):
    """4476130362: two units can share one point; deleting one keeps it."""
    store = _store()
    a = await _ingest(db, store, _body())
    await ingest_knowledge_unit(
        store=store,
        db=db,
        content=_body(),
        project=ro.REFERENCE_PROJECT,
        domain="reference.credentials",
        concept="Lab router admin (copy)",
    )
    assert await ro.delete_reference_entry(db, store, a) is True
    store.delete.assert_not_awaited()
    assert await kc.get(db, a) is None


async def test_an_odd_spaced_placeholder_is_normalised_on_write_and_read(db):
    """4476130420: the canonical placeholder line is what is written and read."""
    unit = await _ingest(db, _store(), _body())
    odd = (await kc.get(db, unit))["body"].replace("Value: ", "Value:   ")
    held, value, _ = ro.split_secret(odd)
    assert value is None
    assert f"\n\nValue: {kc.SECRET_PLACEHOLDER}" in held
    kc.refuse_inline_secret("reference", "reference.credentials", held)
    with pytest.raises(ValueError, match="carries its value"):
        kc.refuse_inline_secret("reference", "reference.credentials", odd)
    row = {**(await kc.get(db, unit)), "body": odd}
    body, missing = await ro.compose_body(db, row)
    assert missing is False
    assert f"\n\nValue: {_VALUE}" in body


async def test_a_direct_upsert_stores_the_credential_key_canonically(db):
    """Review finding: only the ingest chokepoint canonicalised the domain, and
    the readers compare it exactly."""
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")
    unit, _ = await kc.upsert(
        db,
        project_type="Reference",
        domain="Reference.Credentials",
        source_doc="x",
        concept="c",
        body=held,
        secret_value=_VALUE,
    )
    row = await kc.get(db, unit)
    assert (row["project_type"], row["domain"]) == ("reference", "reference.credentials")
    assert await _secret(db, unit) == _VALUE


@pytest.mark.parametrize("existing", [False, True])
async def test_a_failed_body_write_leaves_the_stored_pair_and_one_orphan(db, existing):
    """A unit write that fails leaves the stored value as it was: the new
    secret row is an orphan no unit names, which the sweep removes later."""
    if existing:
        await _ingest(db, _store(), _body())
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")
    with pytest.raises(aiosqlite.IntegrityError):
        await kc.upsert(
            db,
            project_type="reference",
            domain="reference.credentials",
            source_doc=None,
            concept="Lab router admin",
            body=held,
            secret_value="Rotated!Fake-5",
            _commit=False,
        )
    await db.commit()
    if existing:
        unit = (await kc.find_by_unique_key(
            db, project_type="reference", domain="reference.credentials",
            concept="Lab router admin",
        ))["id"]
        assert await _secret(db, unit) == _VALUE
    named = await db.execute_fetchall(
        "SELECT s.value FROM reference_secrets s WHERE NOT EXISTS "
        "(SELECT 1 FROM knowledge_units k WHERE k.secret_ref = s.id)"
    )
    assert [r[0] for r in named] == ["Rotated!Fake-5"]
    assert await _credential_units_without_a_secret(db) == []


