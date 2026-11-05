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
    rows = await db.execute_fetchall(
        "SELECT value FROM reference_secrets WHERE unit_id = ?",
        (unit_id,),
    )
    return rows[0][0] if rows else None


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
    assert await kc.delete(db, unit_id) is True
    assert await _secret(db, unit_id) is None


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
    await db.execute("DELETE FROM knowledge_units WHERE id = ?", (unit_id,))
    assert await _secret(db, unit_id) is None


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


async def test_an_extraction_naming_two_passwords_stores_neither(db):
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import ingest_reference_from_extraction

    ex = Extraction(
        content="Lab router login: username: admin7, password: First!Fake9; backup password: Second!Fake8",
        extraction_type="entity",
        confidence=0.9,
        entities=["Lab router"],
    )
    unit = await ingest_reference_from_extraction(ex, store=_store(), db=db, source_session_id="s")
    body = (await kc.get(db, unit))["body"]
    assert "First!Fake9" not in body and "Second!Fake8" not in body
    assert "admin7" in body  # the login stays searchable
    assert await _secret(db, unit) == "admin7 / First!Fake9"


def test_a_declined_credential_never_falls_through_to_a_url_reference():
    """The user-text gate declines a password Genesis wrote itself; the url
    branch below would have stored the same text, password and all."""
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    ex = Extraction(
        content="Signed in at https://forum.example.org/login with password: Gen!Fake42 for the bot",
        extraction_type="entity",
        confidence=0.9,
    )
    assert classify_as_reference(ex, user_text="please sign the bot in to the forum") is None


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


@pytest.mark.parametrize("phrase", ["password is sunshinefake", "pin is 48151623"])
def test_an_unshaped_password_never_falls_through_to_a_weaker_kind(phrase):
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    content = f"Forum at https://forum.example.org for the bot, username bob, {phrase}"
    ex = Extraction(content=content, extraction_type="entity", confidence=0.9)
    ref = classify_as_reference(ex, user_text="https://forum.example.org")
    assert ref is None or ref["kind"] == "credentials"


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


def test_a_classified_password_keeps_its_trailing_symbol():
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    ex = Extraction(
        content="Lab NAS login: username: nasadmin password: Pw9fake!",
        extraction_type="entity",
        confidence=0.9,
        entities=["Lab NAS"],
    )
    assert classify_as_reference(ex)["value"] == "nasadmin / Pw9fake!"


@pytest.mark.parametrize(
    "raw, value",
    [
        ("Pw9fake)", "Pw9fake)"),
        ("(Pw9fake!)", "Pw9fake!"),
        ('"Pw9fake"', "Pw9fake"),
        ("[Pw9fake]", "Pw9fake"),
        ("(Pw9fake", "Pw9fake"),
        ("Fake9word'", "Fake9word'"),
        ("'Fake9word", "'Fake9word"),
    ],
)
def test_a_capture_loses_only_wrapping_never_its_own_characters(raw, value):
    from genesis.memory.reference_extraction import _unwrap

    assert _unwrap(raw) == value


@pytest.mark.parametrize(
    "content, value",
    [
        ("The lab door keypad: the pin is 48151623 for the bench room", "password: 48151623"),
        ("Old forum account: the password is sunshinefake for the bot", "password: sunshinefake"),
    ],
)
def test_a_wide_rule_capture_the_user_typed_is_kept_as_a_credential(content, value):
    """A secret only ``captured_secrets``' wider rule sees is still kept as a
    credential reference when the user typed it, never dropped."""
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    ex = Extraction(content=content, extraction_type="entity", confidence=0.9)
    ref = classify_as_reference(ex, user_text=content)
    assert ref is not None and ref["kind"] == "credentials"
    assert ref["value"] == value


def test_a_shared_connection_string_is_kept_as_a_credential():
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    content = (
        "Reports database: connect via postgres://admin:Fakepw9@db.example.org:5432/x for jobs"
    )
    ref = classify_as_reference(
        Extraction(content=content, extraction_type="entity", confidence=0.9),
        user_text=content,
    )
    assert ref is not None and ref["kind"] == "credentials"
    assert ref["value"] == "postgres://admin:Fakepw9@db.example.org:5432/x"
    assert "Fakepw9" not in ref["description"]


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
        "AND id NOT IN (SELECT unit_id FROM reference_secrets)"
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
    body lands on ITS row, so the secret is written again under that id, and
    the copy under the losing id is retired."""
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")

    async def _race(conn):
        await conn.execute(
            "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
            "ingested_at) VALUES ('winner', 'reference', 'reference.credentials', 'x', "
            "'Lab router admin', ?, 'now')",
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
    assert await _credential_units_without_a_secret(db) == []


async def test_a_concurrent_delete_never_leaves_a_body_without_its_secret(db):
    """R1, found in review: a delete between the check and the body write takes
    the row and (through the retire trigger) its fresh secret; the body then
    re-creates the row. The secret is written again after the body."""
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
    re-created row would carry no value, so it is removed and the write refused."""
    first = await _ingest(db, _store(), _body())
    held = (await kc.get(db, first))["body"]

    async def _delete(conn):
        await conn.execute("DELETE FROM knowledge_units WHERE id = ?", (first,))

    wrapped = _Conn(db, "INSERT INTO knowledge_units", _delete)
    with pytest.raises(ValueError, match="deleted while this write ran"):
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


@pytest.mark.parametrize("login", ["an", "for", "the"])
def test_a_login_that_spells_a_filler_word_is_kept_after_a_colon(login):
    """R3: the filler-word skip is for prose separators ("login for the bot"),
    not for an explicit "username: X"."""
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    ex = Extraction(
        content=f"Lab NAS login: username: {login}, password: Pw9fake!x",
        extraction_type="entity",
        confidence=0.9,
        entities=["Lab NAS"],
    )
    assert classify_as_reference(ex)["value"] == f"{login} / Pw9fake!x"


def test_a_filler_word_after_a_prose_separator_is_still_skipped():
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    ex = Extraction(
        content="Forum login for the bot persona: username: forumbot password: Pw9fake!x",
        extraction_type="entity",
        confidence=0.9,
        entities=["Forum"],
    )
    assert classify_as_reference(ex)["value"] == "forumbot / Pw9fake!x"


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
    await db.execute(insert, ("ok", "reference", "reference.credentials", "c", held))
    with pytest.raises(aiosqlite.IntegrityError, match="carries its value"):
        await db.execute("UPDATE knowledge_units SET body = ? WHERE id = 'ok'", (_body(),))
    # Another kind keeps its value in the body, and the database lets it.
    await db.execute(insert, ("url", "reference", "reference.url", "u", _body()))


async def test_a_row_written_before_the_change_still_updates_its_counters(db):
    """The UPDATE trigger watches body, project_type and domain only: a legacy
    credential row (value still inline) keeps its retrieval counter working."""
    await db.execute("DROP TRIGGER knowledge_units_credential_body_insert")
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


def test_an_auto_captured_identifier_never_keys_on_the_password():
    """4476130356: with no entities the identifier comes from the text, with
    the password redacted. Two captures that read alike once redacted get
    distinct keys (owner ruling: never overwrite), and the tag that keeps them
    apart is not derived from the value."""
    import hashlib

    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    def ident(password: str) -> str:
        ex = Extraction(
            content=f"Forum bot password: {password} for the weekly job",
            extraction_type="entity",
            confidence=0.9,
        )
        return classify_as_reference(ex)["identifier"]

    first, second = ident("Old!Fake1x"), ident("New!Fake2y")
    assert "Old!Fake1x" not in first
    assert first != second
    assert first.rsplit(" #", 1)[0] == second.rsplit(" #", 1)[0]
    tag = first.rsplit(" #", 1)[1]
    assert tag not in hashlib.sha256(b"Old!Fake1x").hexdigest()


async def test_two_auto_captures_that_read_alike_are_both_kept(db):
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import ingest_reference_from_extraction

    units = []
    for password in ("Old!Fake1x", "New!Fake2y"):
        ex = Extraction(content=f"Forum bot password: {password} for the weekly job",
                        extraction_type="entity", confidence=0.9)
        units.append(await ingest_reference_from_extraction(
            ex, store=_store(), db=db, source_session_id="s",
        ))
    assert units[0] != units[1]
    assert {await _secret(db, u) for u in units} == {"password: Old!Fake1x", "password: New!Fake2y"}


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


@pytest.mark.parametrize(
    "key, credential",
    [
        ("HOTKEY", False),
        ("MONKEY_NAME", False),
        ("API_KEY", True),
        ("OPENAIAPIKEY", True),
        ("AWS_SECRETKEY", True),
        ("DB_PASSWORD", True),
    ],
)
def test_an_env_key_is_a_credential_only_by_whole_part_or_listed_suffix(key, credential):
    """4476130373: no "ends in KEY" test."""
    from genesis.memory.reference_extraction import _env_key_is_credential

    assert _env_key_is_credential(key) is credential


@pytest.mark.parametrize(
    "text, secret",
    [
        ('passphrase: "blue moon river" for the vault', "blue moon river"),
        ("passphrase: 'blue moon river', then sign in", "blue moon river"),
        ("password: \"Pw9fake it's here", '"Pw9fake'),  # no word-ending close: raw
    ],
)
def test_a_quoted_value_is_captured_to_its_closing_quote(text, secret):
    """4476130381."""
    from genesis.memory.reference_extraction import captured_secrets

    assert secret in captured_secrets([text])


@pytest.mark.parametrize(
    "wrapped",
    [
        "`postgres://app:Pw9fakesecret@db.example.org/app`",
        "(postgres://app:Pw9fakesecret@db.example.org/app).",
        '"postgres://app:Pw9fakesecret@db.example.org/app",',
    ],
)
def test_a_connection_string_loses_its_wrapping(wrapped):
    """4476136574: a matched pair around the URL is never part of the value."""
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    content = f"Reports database: connect via {wrapped} for the nightly jobs"
    ref = classify_as_reference(
        Extraction(
            content=content, extraction_type="entity", confidence=0.9, entities=["Reports db"]
        )
    )
    assert ref["value"] == "postgres://app:Pw9fakesecret@db.example.org/app"


@pytest.mark.parametrize(
    "text, secret",
    [
        ("Set NODE_ENV=production before deploy", None),
        ("MODEL_NAME=qwen3-embedding:0.6b-fp16 is the embedder", None),
        ("AUTHOR=someone CAPITAL_CITY=Lisbonfake API_BASE=v2alpha-fake", None),
        ("Put API_KEY=abcd1234efgh5678 in the env file", "abcd1234efgh5678"),
        ("the password is Hunt3r!fake for now", "Hunt3r!fake"),
        ("my pin is 48151623 at the desk", "48151623"),
        ("the api key is abcdefghijklmnopqrstuv here", "abcdefghijklmnopqrstuv"),
        ("DATABASE_URL=postgres://app:Pw9fakesecret@db.example.org/app", "Pw9fakesecret"),
        ("cache at redis://:s3cretfake@cache:6379/0 for jobs", "s3cretfake"),
    ],
)
def test_captured_secrets_sees_only_credential_shapes(text, secret):
    from genesis.memory.reference_extraction import captured_secrets

    found = captured_secrets([text])
    assert (secret in found) if secret else not found


def test_an_empty_quoted_password_never_breaks_the_classifier():
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import captured_secrets, classify_as_reference

    text = 'u: bob password: "" and later API_TOKEN=Fake9wordZ1xyz in the env file here'
    classify_as_reference(Extraction(content=text, extraction_type="entity", confidence=0.9))
    assert "Fake9wordZ1xyz" in captured_secrets([text])


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
async def test_a_failed_body_write_puts_the_secret_back(db, existing):
    """Review finding: the secret is written first, so a body write that fails
    must not leave the stored value rotated (or a new key's value orphaned)."""
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
    values = [r[0] for r in await db.execute_fetchall("SELECT value FROM reference_secrets")]
    assert values == ([_VALUE] if existing else [])


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
