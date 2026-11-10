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
    # The description's repeat of the password is redacted; the prose stays.
    assert f"Router login, password is {ro.REDACTED}" in row["body"]
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


async def test_the_tags_lose_the_value_too(db):
    store = _store()
    unit_id = await _ingest(
        db, store, _body(tags=["router", _PASSWORD]), tags=["router", _PASSWORD]
    )
    row = await kc.get(db, unit_id)
    assert _PASSWORD not in row["tags"]
    assert _PASSWORD not in row["body"]
    assert json.loads(row["tags"]) == ["router", ro.REDACTED]


# ─── which fragments are secret ──────────────────────────────────────────────


def test_identifiers_inside_a_value_stay_searchable():
    frags = ro.secret_fragments(
        "user: example-admin, email: ops@example.org, password: Hunter2!xyz-fake"
    )
    assert "Hunter2!xyz-fake" in frags
    assert "example-admin" not in frags
    assert "ops@example.org" not in frags


def test_a_short_unlabelled_token_is_not_scrubbed_from_prose():
    """'admin / Hunter2!xyz': redacting 'admin' would redact 'router admin panel'."""
    held, value, _, _ = ro.split_secret(
        _body(value="admin / Hunter2!xyz", description="Router admin panel")
    )
    assert value == "admin / Hunter2!xyz"
    assert "Router admin panel" in held


def test_a_login_slash_secret_value_keeps_the_login_searchable():
    frags = ro.secret_fragments("ForumUser42 / hunter2-fake")
    assert "hunter2-fake" in frags
    assert "ForumUser42" not in frags
    # Three parts is not the convention: every long enough part is held out.
    assert "ForumUser42" in ro.secret_fragments("ForumUser42 / hunter2-fake / 4242-fake")


def test_the_whole_value_is_always_a_fragment():
    assert ro.secret_fragments("abc")[0] == "abc"


# ─── refusals and re-ingest ──────────────────────────────────────────────────


async def test_a_credential_without_a_value_line_is_refused_before_any_write(db):
    store = _store()
    with pytest.raises(ValueError, match="Value:"):
        await _ingest(
            db, store, f"[reference.credentials] Lab router admin\n\npassword {_PASSWORD}"
        )
    store.store.assert_not_awaited()
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_units") == []


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


def test_a_password_inside_a_prose_value_is_redacted_from_the_description():
    held, _, _, _ = ro.split_secret(
        _body(
            value="log in as example-admin with password k9!Lp-fake",
            description="Router login; the password k9!Lp-fake was rotated",
        )
    )
    assert "k9!Lp-fake" not in held
    assert "the password [redacted] was rotated" in held


def test_space_separated_pairs_and_a_short_slash_secret_are_held():
    frags = ro.secret_fragments("password=Abc123!x token=Zz9_fake")
    assert {"Abc123!x", "Zz9_fake"} <= set(frags)
    assert "k9!Lp" in ro.secret_fragments("opsbot / k9!Lp")


def test_a_fragment_inside_a_longer_word_is_left_alone():
    assert ro.scrub("the inactive flag", ["active"]) == "the inactive flag"
    assert ro.scrub("is active now", ["active"]) == f"is {ro.REDACTED} now"


def test_plain_words_in_a_prose_value_are_never_redacted():
    frags = ro.secret_fragments("log in as admin with password k9!Lp-fake")
    for word in ("password", "with", "admin"):
        assert word not in frags


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


def test_a_tag_loses_the_value_even_inside_a_longer_word():
    """Prose keeps word boundaries; a tag does not need them (a live tag carried
    its password that way)."""
    held, _, tags, _ = ro.split_secret(
        _body(tags=[f"pw{_PASSWORD}x"]),
        json.dumps([f"pw{_PASSWORD}x"]),
    )
    assert _PASSWORD not in tags
    assert _PASSWORD not in held


# ─── review round 1 (#3898) ──────────────────────────────────────────────────


async def test_the_concept_loses_the_value_too(db):
    """An extraction with no entities derives the identifier from its own text,
    which can carry the password; the concept is indexed in knowledge_fts."""
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
    fts = await db.execute_fetchall(
        "SELECT concept FROM knowledge_fts WHERE unit_id = ?", (unit_id,)
    )
    assert _PASSWORD not in row["concept"] and _PASSWORD not in fts[0][0]
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


def test_a_description_carrying_the_value_marker_is_refused():
    with pytest.raises(ValueError, match="exactly one"):
        ro.split_secret(_body(description="note\n\nValue: tail-fake"))


async def test_a_new_credential_with_no_value_to_hold_is_refused(db):
    """A held-out body under a NEW identifier (or a value equal to the
    placeholder) brings no secret: refused, nothing written."""
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")
    with pytest.raises(ValueError, match="no value to hold"):
        await ingest_knowledge_unit(
            store=_store(),
            db=db,
            content=held,
            project=ro.REFERENCE_PROJECT,
            domain="reference.credentials",
            concept="brand new",
        )
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_units") == []


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


def test_glued_short_and_multiword_secrets_are_all_held():
    assert "xy" in ro.secret_fragments("operator / xy")
    assert ro.scrub("prefixHunter2!xyzsuffix", ["Hunter2!xyz"]) == f"prefix{ro.REDACTED}suffix"
    frags = ro.secret_fragments("password: blue moon river")
    assert {"blue", "moon", "river"} <= set(frags)
    held, _, _, _ = ro.split_secret(
        _body(value="password: blue moon river", description="the blue moon one")
    )
    assert "blue moon" not in held


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


def test_a_labelled_all_letter_password_is_redacted_even_glued():
    """A labelled secret IS the secret: word boundaries protect prose words
    ("inactive"), never a password run into other text."""
    held, _, _, _ = ro.split_secret(
        _body(value="password: sunshinefake", description="old: xsunshinefakes")
    )
    assert "sunshinefake" not in held
    assert "inactive" in ro.scrub("inactive", ["active"])


# ── Round 2: every captured secret, every reference kind, checks before any store ──


async def test_a_refused_credential_stores_no_vector(db):
    """The refusal runs before store.store, so no orphaned point or memory_fts
    row names a unit that was never written."""
    store = _store()
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")
    with pytest.raises(ValueError, match="no value to hold"):
        await ingest_knowledge_unit(
            store=store,
            db=db,
            content=held,
            project=ro.REFERENCE_PROJECT,
            domain="reference.credentials",
            concept="brand new",
        )
    store.store.assert_not_awaited()


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
async def test_distillation_refuses_a_credential_unit_before_any_vector(
    memory_mcp_store,
    tmp_path,
    domain,
):
    """4476130424: behaviour, not source order."""
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator

    orch = KnowledgeOrchestrator(
        registry=MagicMock(),
        distillation=MagicMock(),
        manifest=ManifestManager(root=tmp_path / "knowledge"),
    )
    unit = MagicMock()
    unit.domain = domain
    with pytest.raises(ValueError, match="credential"):
        await orch._store_units(
            [unit],
            project_type="reference",
            source="notes.md",
            content=MagicMock(),
            purpose=None,
        )
    memory_mcp_store.store.assert_not_awaited()


@pytest.mark.parametrize("domain", ["reference.credentials", "Reference.Credentials"])
async def test_an_upload_refuses_a_credential_domain_before_any_vector(
    memory_mcp_store,
    tmp_path,
    domain,
):
    from genesis.knowledge.ingest_upload import _store_as_is

    upload = tmp_path / "creds.txt"
    upload.write_text("some uploaded text")
    with pytest.raises(ValueError, match="credential"):
        await _store_as_is(
            str(upload),
            "creds.txt",
            project_type="reference",
            domain=domain,
            purpose=None,
            context="",
        )
    memory_mcp_store.store.assert_not_awaited()


class _Conn:
    """A connection whose ``hook`` runs before each statement it matches, to
    stand in for what another coroutine does on the shared connection between
    two of ours."""

    def __init__(self, conn, prefix: str, hook):
        self._conn, self._prefix, self._hook, self.fired = conn, prefix, hook, 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args, **kwargs):
        # Returned unawaited, as aiosqlite's own execute is, so the callers that
        # use it as a context manager still can.
        if not sql.lstrip().startswith(self._prefix) or self.fired:
            return self._conn.execute(sql, *args, **kwargs)
        self.fired += 1

        async def _hooked():
            await self._hook(self._conn)
            return await self._conn.execute(sql, *args, **kwargs)

        return _hooked()


async def _credential_units_without_a_secret(db) -> list:
    return await db.execute_fetchall(
        "SELECT id FROM knowledge_units WHERE domain = 'reference.credentials' "
        "AND (secret_ref IS NULL OR secret_ref NOT IN (SELECT id FROM reference_secrets))"
    )


@pytest.mark.parametrize("existing", [False, True])
async def test_a_stop_between_the_secret_and_the_body_leaves_no_body_without_its_secret(
    db,
    existing,
):
    """R1: a cancellation before ANY write of the upsert (enumerated: the first,
    the second, ... the last), then a commit by another writer, never leaves a
    credential body without its secret."""
    import asyncio

    if existing:
        await _ingest(db, _store(), _body())
    new_value = "user: example-admin, password: Next!Fake-4"

    class _CancelAt:
        def __init__(self, conn, n):
            self._conn, self._n, self.writes = conn, n, 0

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *args, **kwargs):
            if sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
                self.writes += 1
                if self.writes == self._n:

                    async def _stop():
                        raise asyncio.CancelledError

                    return _stop()
            return self._conn.execute(sql, *args, **kwargs)

    async def _run(conn):
        await kc.upsert(
            conn,
            project_type="reference",
            domain="reference.credentials",
            source_doc="x",
            concept="Lab router admin",
            body=_body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}"),
            secret_value=new_value,
            _commit=False,
        )

    stopped = 0
    for n in range(1, 10):
        conn = _CancelAt(db, n)
        try:
            await _run(conn)
        except asyncio.CancelledError:
            stopped += 1
        await db.commit()  # another coroutine's commit on the shared connection
        assert await _credential_units_without_a_secret(db) == [], f"stopped at write {n}"
        if conn.writes < n:
            break  # this run had fewer writes than n: every stop point was covered
        # Start each stop point from the same state.
        await db.execute("DELETE FROM knowledge_units WHERE concept = 'Lab router admin'")
        await db.execute("DELETE FROM reference_secrets")
        await db.execute("DELETE FROM knowledge_fts")
        if existing:
            await _ingest(db, _store(), _body())
        await db.commit()
    assert stopped >= 3  # the secret, the body and its FTS row were each a stop point


async def test_a_concurrent_insert_of_the_same_key_gets_the_secret_too(db):
    """R1: another writer inserts the key between the check and the write. The
    body lands on ITS row in the same statement that repoints it to this
    writer's secret, and the other writer's secret is retired."""
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")

    async def _race(conn):
        await conn.execute(
            "INSERT INTO reference_secrets (id, value, created_at) VALUES ('w', 'other-Fake1', 'now')"
        )
        await conn.execute(
            "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
            "ingested_at, secret_ref) VALUES ('winner', 'reference', 'reference.credentials', 'x', "
            "'Lab router admin', ?, 'now', 'w')",
            (held,),
        )

    wrapped = _Conn(db, "INSERT INTO knowledge_units", _race)
    actual, inserted = await kc.upsert(
        wrapped,
        id="loser",
        project_type="reference",
        domain="reference.credentials",
        source_doc="x",
        concept="Lab router admin",
        body=held,
        secret_value=_VALUE,
    )
    assert wrapped.fired == 1
    assert (actual, inserted) == ("winner", False)
    assert await _secret(db, "winner") == _VALUE
    assert await _secret(db, "loser") is None
    assert await _secret_rows(db) == 1  # the other writer's secret retired
    assert await _credential_units_without_a_secret(db) == []


async def test_a_concurrent_delete_never_leaves_a_body_without_its_secret(db):
    """R1, found in review: a delete between the check and the body write takes
    the row and its old secret; the unit write then re-creates the row, naming
    the fresh secret this writer inserted, which the delete never touched."""
    first = await _ingest(db, _store(), _body())
    held = (await kc.get(db, first))["body"]

    async def _delete(conn):
        await conn.execute("DELETE FROM knowledge_units WHERE id = ?", (first,))

    wrapped = _Conn(db, "INSERT INTO knowledge_units", _delete)
    await kc.upsert(
        wrapped,
        id=first,
        project_type="reference",
        domain="reference.credentials",
        source_doc="x",
        concept="Lab router admin",
        body=held,
        secret_value="rotated-Fake9!",
    )
    assert wrapped.fired == 1
    assert await _credential_units_without_a_secret(db) == []
    assert await _secret(db, first) == "rotated-Fake9!"


async def test_a_value_free_re_ingest_racing_a_delete_is_undone_and_refused(db):
    """A held-out body re-ingested with no value, while a delete runs: the
    re-created row would name a secret the delete retired, so the secret_ref
    trigger refuses the write and nothing is re-created."""
    first = await _ingest(db, _store(), _body())
    held = (await kc.get(db, first))["body"]

    async def _delete(conn):
        # As knowledge_crud.delete does it: the unit and its FTS row.
        await conn.execute("DELETE FROM knowledge_units WHERE id = ?", (first,))
        await conn.execute("DELETE FROM knowledge_fts WHERE unit_id = ?", (first,))

    wrapped = _Conn(db, "INSERT INTO knowledge_units", _delete)
    with pytest.raises(ValueError, match="deleted or repointed while this write ran"):
        await kc.upsert(
            wrapped,
            id=first,
            project_type="reference",
            domain="reference.credentials",
            source_doc="x",
            concept="Lab router admin",
            body=held,
        )
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_units") == []
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_fts") == []


def test_a_connection_string_password_is_its_own_fragment():
    """R2: the URL's userinfo password is held out on its own, so a description
    naming just the password loses it."""
    url = "postgres://app:Pw9fakesecret@db.example.org/app"
    frags = ro.secret_fragments(url)
    assert "Pw9fakesecret" in frags
    held, _, _, _ = ro.split_secret(
        _body(value=url, description="the app password is Pw9fakesecret")
    )
    assert "Pw9fakesecret" not in held


def test_an_email_shaped_password_is_held_but_an_email_login_is_not():
    """R2: the email-shaped exclusion applies to identifier labels only."""
    frags = ro.secret_fragments("email: ops@example.org, password: fakepass@example.org")
    assert "fakepass@example.org" in frags
    assert "ops@example.org" not in frags
    held, _, _, _ = ro.split_secret(
        _body(
            value="password: fakepass@example.org", description="sign in with fakepass@example.org"
        )
    )
    assert "fakepass@example.org" not in held


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
    held, value, _, _ = ro.split_secret(odd)
    assert value is None
    assert f"\n\nValue: {kc.SECRET_PLACEHOLDER}" in held
    kc.refuse_inline_secret("reference", "reference.credentials", held)
    with pytest.raises(ValueError, match="carries its value"):
        kc.refuse_inline_secret("reference", "reference.credentials", odd)
    row = {**(await kc.get(db, unit)), "body": odd}
    body, missing = await ro.compose_body(db, row)
    assert missing is False
    assert f"\n\nValue: {_VALUE}" in body


def test_a_short_labelled_secret_is_redacted_only_as_a_word():
    """4476136578: a 1-3 character labelled secret, or a 1-character pair
    secret, is held out, but never cut out of longer text."""
    held, _, tags, _ = ro.split_secret(
        _body(value="user: bob, password: x9z", description="pin x9z, not ax9zb"),
        json.dumps(["x9z", "tax9zone"]),
    )
    assert "pin [redacted], not ax9zb" in held
    assert json.loads(tags) == [ro.REDACTED, "tax9zone"]
    held, _, _, _ = ro.split_secret(_body(value="bob / q", description="bob, q, quick"))
    assert "bob, [redacted], quick" in held


async def test_the_authority_and_provenance_lose_the_value_too(db):
    """3dccd364: captured_via goes into authority, source_doc and
    source_platform, and authority reaches the vector payload's tags."""
    store = _store()
    via = f"chat {_PASSWORD}"
    unit = await ingest_knowledge_unit(
        store=store,
        db=db,
        content=_body(source={"captured_via": via}),
        project=ro.REFERENCE_PROJECT,
        domain="reference.credentials",
        concept="Lab router admin",
        authority=via,
        provenance={
            "source_doc": f"reference_store:{via}",
            "platform": via,
            "source_pipeline": "reference_store",
        },
    )
    row = await kc.get(db, unit)
    for text in (
        row["source_doc"],
        row["source_platform"],
        row["body"],
        *store.store.await_args.kwargs["tags"],
    ):
        assert _PASSWORD not in (text or ""), text
    assert row["source_doc"] == f"reference_store:chat {ro.REDACTED}"


def test_a_repeat_of_the_value_in_another_case_is_redacted_too():
    """Review finding: the repeat redaction ignored case."""
    held, _, tags, _ = ro.split_secret(
        _body(value="password: Hunter2!fake", description="pw HUNTER2!FAKE, also sunflower"),
        json.dumps(["hunter2!fake"]),
    )
    assert "hunter2!fake" not in held.lower()
    assert json.loads(tags) == [ro.REDACTED]


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


def test_a_connection_string_password_with_an_at_sign_is_held_whole():
    url = "postgres://app:p@ss-fake9@db.example.org/x"
    assert "p@ss-fake9" in ro.secret_fragments(url)
    held, _, _, _ = ro.split_secret(_body(value=url, description="pw p@ss-fake9"))
    assert "ss-fake9" not in held


def test_a_quoted_labelled_secret_is_held_without_its_quotes():
    held, _, _, _ = ro.split_secret(
        _body(value='user: bob, secret: "two words"', description="it is two words long"),
    )
    assert "two words" not in held


def test_the_userinfo_scheme_scan_is_bounded():
    """Review finding: an unbounded scheme scanned quadratically on a long run
    of scheme characters. The scheme is bounded, so a long run never matches."""
    assert ro.URL_USERINFO_PATTERN.search("a" * 40 + "://u:pw9fake@h") is None
    assert ro.URL_USERINFO_PATTERN.search("postgres://u:pw9fake@h").group("pw") == "pw9fake"
# ─── the pairing invariant, over every ordering (rework v2) ──────────────────
#
# The shared connection serializes STATEMENTS, not transactions, so what two
# writers of one key can do to each other is fixed by the order their
# statements reach it. ``_Stepped`` makes each writer wait for its turn before
# every statement, so a schedule (a sequence of writer names) is one ordering;
# the tests run every ordering of two writers, of a writer and a delete, of a
# writer and the orphan sweep, and of a writer cancelled at each await point
# against another, then check the INVARIANT: every credential unit names a
# secret row, that row holds the value of the writer whose body the unit
# carries, and any other secret row is an orphan no unit names.


class _Schedule:
    """Hands out statement turns in the order of ``order``; a writer whose
    entries run out, or that has finished, runs free."""

    def __init__(self, order: list[str]):
        import asyncio

        self._order = list(order)
        self._done: set[str] = set()
        self._changed = asyncio.Condition()

    def _next(self) -> str | None:
        while self._order and self._order[0] in self._done:
            self._order.pop(0)
        return self._order[0] if self._order else None

    async def acquire(self, name: str) -> None:
        async with self._changed:
            await self._changed.wait_for(
                lambda: self._next() in (name, None) or name not in self._order
            )

    async def release(self, name: str) -> None:
        async with self._changed:
            if self._next() == name:
                self._order.pop(0)
            self._changed.notify_all()

    async def finish(self, name: str) -> None:
        async with self._changed:
            self._done.add(name)
            self._changed.notify_all()


class _Stepped:
    """``db`` for one writer: each statement waits for that writer's turn. With
    ``cancel_at=(k, when)`` the writer is cancelled at its k-th statement,
    ``before`` it runs or ``after`` it ran."""

    def __init__(self, db, name: str, schedule: _Schedule, cancel_at=None):
        self._db, self.name, self._schedule = db, name, schedule
        self._cancel_at, self.steps = cancel_at, 0

    def __getattr__(self, attr):
        return getattr(self._db, attr)

    async def _step(self, run):
        import asyncio

        self.steps += 1
        await self._schedule.acquire(self.name)
        try:
            if self._cancel_at == (self.steps, "before"):
                raise asyncio.CancelledError
            result = await run()
            if self._cancel_at == (self.steps, "after"):
                raise asyncio.CancelledError
            return result
        finally:
            await self._schedule.release(self.name)

    async def execute(self, sql, params=()):
        return await self._step(lambda: self._db.execute(sql, params))

    async def execute_fetchall(self, sql, params=()):
        return await self._step(lambda: self._db.execute_fetchall(sql, params))


_KEY = "Lab router admin"
_HELD = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")


def _value_of(writer: str) -> str:
    return f"password: {writer}-Fake9x"


async def _write(conn, writer: str, *, insert_only: bool = False):
    """One credential upsert of the key; ``source_doc`` names the body's writer."""
    return await kc.upsert(
        conn,
        project_type="reference",
        domain="reference.credentials",
        source_doc=f"writer-{writer}",
        concept=_KEY,
        body=_HELD,
        secret_value=_value_of(writer),
        insert_only=insert_only,
    )


async def _delete(conn, _writer: str):
    rows = await conn.execute_fetchall(
        "SELECT id FROM knowledge_units WHERE concept = ?",
        (_KEY,),
    )
    if rows:
        await kc.delete(conn, rows[0][0])


async def _sweep_now(conn, _writer: str):
    """The sweep with no age bound: it may take a secret whose unit is on its way."""
    await kc.sweep_orphan_secrets(conn, older_than_s=-60)


async def _new_shape_pairs(db) -> list[tuple[str, str | None]]:
    rows = await db.execute_fetchall(
        "SELECT k.source_doc, s.value FROM knowledge_units k "
        "LEFT JOIN reference_secrets s ON s.id = k.secret_ref WHERE k.concept = ?",
        (_KEY,),
    )
    return [(r[0].removeprefix("writer-"), r[1]) for r in rows]


async def _assert_invariant(db, pairs_fn=_new_shape_pairs, label="") -> None:
    pairs = await pairs_fn(db)
    assert len(pairs) <= 1, label
    for body_writer, value in pairs:
        assert value == _value_of(body_writer), f"{label}: body {body_writer} names {value!r}"
    if pairs_fn is _new_shape_pairs:
        # Every other secret row is an orphan, and no unit names a missing one.
        assert await _credential_units_without_a_secret(db) == [], label


async def _reset(db, seed: str | None) -> None:
    await db.execute("DELETE FROM knowledge_units")
    await db.execute("DELETE FROM knowledge_fts")
    await db.execute("DELETE FROM reference_secrets")
    await db.commit()
    if seed:
        await _write(db, seed)


async def _steps_of(db, op, name, seed) -> int:
    """How many statements ``op`` runs alone from the seeded state."""
    await _reset(db, seed)
    schedule = _Schedule([])
    conn = _Stepped(db, name, schedule)
    await op(conn, name)
    return conn.steps


def _orderings(a: str, n_a: int, b: str, n_b: int):
    import itertools

    for slots in itertools.combinations(range(n_a + n_b), n_a):
        order = [b] * (n_a + n_b)
        for i in slots:
            order[i] = a
        yield order


async def _run(db, ops, order, seed, cancel=None) -> dict:
    """Run ``ops`` ({name: op}) under one ordering; return {name: outcome}."""
    import asyncio

    await _reset(db, seed)
    schedule = _Schedule(order)

    async def one(name, op):
        conn = _Stepped(db, name, schedule, cancel.get(name) if cancel else None)
        try:
            return await op(conn, name)
        finally:
            await schedule.finish(name)

    names = list(ops)
    results = await asyncio.gather(
        *(one(n, ops[n]) for n in names),
        return_exceptions=True,
    )
    await db.commit()  # another coroutine's commit on the shared connection
    return dict(zip(names, results, strict=True))


async def _sweep_orderings(
    db, ops: dict, seed, pairs_fn=_new_shape_pairs, cancel=None, n_a: int | None = None,
) -> int:
    """Every ordering of the two ops' statements; returns how many ran. ``n_a``
    caps the first op's statements (a writer cancelled at its k-th runs k)."""
    (a, op_a), (b, op_b) = ops.items()
    n_a = n_a or await _steps_of(db, op_a, a, seed)
    n_b = await _steps_of(db, op_b, b, seed)
    runs = 0
    for order in _orderings(a, n_a, b, n_b):
        outcome = await _run(db, ops, order, seed, cancel)
        for result in outcome.values():
            # A cancellation is the one failure a run may show.
            if isinstance(result, BaseException) and not isinstance(result, asyncio_cancelled()):
                raise result
        await _assert_invariant(db, pairs_fn, f"{order} {cancel}")
        runs += 1
    return runs


def asyncio_cancelled():
    import asyncio

    return asyncio.CancelledError


@pytest.mark.parametrize("seed", [None, "S"])
async def test_every_ordering_of_two_writers_keeps_each_body_with_its_secret(db, seed):
    """4482018636, as an invariant: two upserts of one key, every ordering of
    their statements, from an absent key and from an existing one."""
    runs = await _sweep_orderings(db, {"A": _write, "B": _write}, seed)
    assert runs >= 20  # C(n_a + n_b, n_a) orderings ran, not a degenerate few


@pytest.mark.parametrize("seed", [None, "S"])
async def test_every_ordering_of_a_writer_and_a_delete_keeps_the_pair(db, seed):
    runs = await _sweep_orderings(db, {"A": _write, "D": _delete}, seed)
    assert runs >= 7  # an absent key: the delete is one read, at each of A's 7 gaps


@pytest.mark.parametrize("seed", [None, "S"])
async def test_a_sweep_that_takes_a_young_secret_refuses_the_unit_never_strands_it(db, seed):
    """The sweep with no age bound, at every point of a writer: where it takes
    the writer's new secret before the unit lands, the unit write is refused
    (the caller sees a failure), and no unit is left without its secret."""
    a, n_a = "A", await _steps_of(db, _write, "A", seed)
    n_w = await _steps_of(db, _sweep_now, "W", seed)
    refused = 0
    for order in _orderings(a, n_a, "W", n_w):
        outcome = await _run(db, {"A": _write, "W": _sweep_now}, order, seed)
        if isinstance(outcome["A"], ValueError):
            refused += 1
        elif isinstance(outcome["A"], BaseException):
            raise outcome["A"]
        await _assert_invariant(db, label=str(order))
    assert refused >= 1  # the window was really hit, and refused


@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("seed", [None, "S"])
async def test_a_writer_cancelled_at_each_await_point_never_strands_or_crosses(db, seed, when):
    """A cancelled at its k-th statement (before it runs, or after), for every
    k, against a full writer B, in every ordering of their statements."""
    n_a = await _steps_of(db, _write, "A", seed)
    for k in range(1, n_a + 1):
        await _sweep_orderings(
            db,
            {"A": _write, "B": _write},
            seed,
            cancel={"A": (k, when)},
            n_a=k,
        )


async def _old_shape_write(conn, writer: str):
    """The two-table shape this replaced: the body first, then the secret keyed
    by the unit id, in separate statements."""
    await conn.execute(
        "INSERT INTO old_secrets_units (concept, source_doc) VALUES (?, ?) "
        "ON CONFLICT(concept) DO UPDATE SET source_doc = excluded.source_doc",
        (_KEY, f"writer-{writer}"),
    )
    rows = await conn.execute_fetchall(
        "SELECT rowid FROM old_secrets_units WHERE concept = ?",
        (_KEY,),
    )
    await conn.execute(
        "INSERT INTO old_secrets (unit_id, value) VALUES (?, ?) "
        "ON CONFLICT(unit_id) DO UPDATE SET value = excluded.value",
        (rows[0][0], _value_of(writer)),
    )


async def _old_shape_pairs(db) -> list[tuple[str, str | None]]:
    rows = await db.execute_fetchall(
        "SELECT u.source_doc, s.value FROM old_secrets_units u "
        "LEFT JOIN old_secrets s ON s.unit_id = u.rowid",
    )
    return [(r[0].removeprefix("writer-"), r[1]) for r in rows]


async def test_control_the_old_two_table_shape_fails_the_invariant(db):
    """The harness can see a crossing: the shape this replaced (secret keyed by
    the unit id, written after the body) breaks the invariant in some ordering
    of two writers."""
    await db.execute("CREATE TABLE old_secrets_units (concept TEXT UNIQUE, source_doc TEXT)")
    await db.execute("CREATE TABLE old_secrets (unit_id INTEGER PRIMARY KEY, value TEXT)")
    n = await _steps_of(db, _old_shape_write, "A", None)
    crossed = 0
    for order in _orderings("A", n, "B", n):
        await db.execute("DELETE FROM old_secrets_units")
        await db.execute("DELETE FROM old_secrets")
        await _run(db, {"A": _old_shape_write, "B": _old_shape_write}, order, None)
        try:
            await _assert_invariant(db, _old_shape_pairs)
        except AssertionError:
            crossed += 1
    assert crossed >= 1


# ─── the orphan sweep and the secret_ref triggers ────────────────────────────


async def _raw_secret(db, ref: str, value: str, created_at: str) -> None:
    await db.execute(
        "INSERT INTO reference_secrets (id, value, created_at) VALUES (?, ?, ?)",
        (ref, value, created_at),
    )


async def test_the_sweep_keeps_young_orphans_and_named_rows(db):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    old = (now - timedelta(hours=2)).isoformat()
    young = (now - timedelta(minutes=5)).isoformat()
    await _raw_secret(db, "old-orphan", "fake-1", old)
    await _raw_secret(db, "young-orphan", "fake-2", young)
    unit = await _ingest(db, _store(), _body())
    named = (await kc.get(db, unit))["secret_ref"]
    await db.execute(
        "UPDATE reference_secrets SET created_at = ? WHERE id = ?",
        (old, named),
    )
    assert await kc.sweep_orphan_secrets(db) == 1
    left = {r[0] for r in await db.execute_fetchall("SELECT id FROM reference_secrets")}
    assert left == {"young-orphan", named}
    assert await _secret(db, unit) == _VALUE


async def test_delete_runs_the_sweep(db):
    from datetime import UTC, datetime, timedelta

    await _raw_secret(
        db,
        "old-orphan",
        "fake-1",
        (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    unit = await _ingest(db, _store(), _body())
    await kc.delete(db, unit)
    assert await _secret_rows(db) == 0


_RAW_UNIT = (
    "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
    "ingested_at, secret_ref) VALUES (?, 'reference', 'reference.credentials', 'x', ?, ?, "
    "'now', ?)"
)


async def test_a_credential_unit_naming_no_secret_is_refused(db):
    for ref in (None, "no-such-row"):
        with pytest.raises(aiosqlite.IntegrityError, match="names no stored secret"):
            await db.execute(_RAW_UNIT, ("u", "c", _HELD, ref))
    await _raw_secret(db, "s1", "fake-1", "now")
    await db.execute(_RAW_UNIT, ("u", "c", _HELD, "s1"))
    for ref in (None, "no-such-row"):
        with pytest.raises(aiosqlite.IntegrityError, match="names no stored secret"):
            await db.execute("UPDATE knowledge_units SET secret_ref = ? WHERE id = 'u'", (ref,))
    # Another kind is not a credential, and needs no secret.
    await db.execute(
        "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
        "ingested_at) VALUES ('n', 'reference', 'reference.network', 'x', 'n', 'b', 'now')"
    )


async def test_repointing_or_deleting_a_unit_retires_its_secret_unless_shared(db):
    for ref in ("s1", "s2"):
        await _raw_secret(db, ref, f"fake-{ref}", "now")
    await db.execute(_RAW_UNIT, ("u1", "c1", _HELD, "s1"))
    await db.execute(_RAW_UNIT, ("u2", "c2", _HELD, "s1"))  # a shared ref
    await db.execute("UPDATE knowledge_units SET secret_ref = 's2' WHERE id = 'u1'")
    ids = lambda rows: {r[0] for r in rows}  # noqa: E731
    assert ids(await db.execute_fetchall("SELECT id FROM reference_secrets")) == {"s1", "s2"}
    await db.execute("DELETE FROM knowledge_units WHERE id = 'u2'")
    assert ids(await db.execute_fetchall("SELECT id FROM reference_secrets")) == {"s2"}
    await db.execute("DELETE FROM knowledge_units WHERE id = 'u1'")
    assert await _secret_rows(db) == 0


async def test_an_upsert_with_a_new_value_repoints_and_retires_the_old_secret(db):
    unit = await _ingest(db, _store(), _body())
    first_ref = (await kc.get(db, unit))["secret_ref"]
    await _ingest(db, _store(), _body(value="user: example-admin, password: Next!Fake-7"))
    row = await kc.get(db, unit)
    assert row["secret_ref"] != first_ref
    assert await _secret_rows(db) == 1
    assert len(row["secret_ref"]) == 32  # token_hex(16): random, not from the value


# ─── insert-only auto-capture (4484515141) ───────────────────────────────────


async def test_every_ordering_of_two_insert_only_writers_keeps_both_values_safe(db):
    """Two auto-captures of one key with different values: in every ordering
    exactly one lands on the key, the other is told the key is taken (and
    writes nothing), and the key's secret is the lander's."""
    ops = {
        "A": lambda c, n: _write(c, n, insert_only=True),
        "B": lambda c, n: _write(c, n, insert_only=True),
    }
    n_a = await _steps_of(db, ops["A"], "A", None)
    n_b = await _steps_of(db, ops["B"], "B", None)
    for order in _orderings("A", n_a, "B", n_b):
        outcome = await _run(db, ops, order, None)
        taken = [n for n, r in outcome.items() if isinstance(r, kc.CredentialKeyTaken)]
        landed = [n for n, r in outcome.items() if not isinstance(r, BaseException)]
        assert len(taken) == 1 and len(landed) == 1, (order, outcome)
        await _assert_invariant(db, label=str(order))
        assert (await _new_shape_pairs(db))[0][0] == landed[0]
        assert await _secret_rows(db) == 1  # the taken writer left no orphan


async def test_an_insert_only_write_never_changes_an_existing_key(db):
    await _write(db, "A")
    with pytest.raises(kc.CredentialKeyTaken):
        await _write(db, "B", insert_only=True)
    assert await _new_shape_pairs(db) == [("A", _value_of("A"))]
    # The same value is a no-op success.
    unit, inserted = await _write(db, "A", insert_only=True)
    assert inserted is False
    assert await _secret(db, unit) == _value_of("A")
    assert await _secret_rows(db) == 1
