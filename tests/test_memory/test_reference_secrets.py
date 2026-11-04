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
        _body(value="log in as example-admin with password k9!Lp-fake",
              description="Router login; the password k9!Lp-fake was rotated")
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
            await kc.upsert(db, project_type="reference", domain=domain,
                            source_doc="x", concept="c", body=_body())
        with pytest.raises(ValueError, match="carries its value"):
            await kc.insert(db, project_type="reference", domain=domain,
                            source_doc="x", concept="c", body=_body())


async def test_an_odd_domain_spelling_is_held_out_not_refused(db):
    unit_id = await _ingest(db, _store(), _body(), domain="Reference.Credentials")
    assert await _secret(db, unit_id) == _VALUE


def test_an_extraction_naming_a_credential_is_scrubbed_before_it_is_stored():
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import scrub_credential_extraction

    ex = Extraction(
        content="Lab router login: username: example-admin, password: Hunter2!xyz-fake",
        extraction_type="entity",
        confidence=0.9,
        entities=["lab router", "Hunter2!xyz-fake"],
        relationships=[{"subject": "router", "object": "Hunter2!xyz-fake"}],
    )
    assert scrub_credential_extraction(ex) is True
    for text in (ex.content, *ex.entities, *ex.relationships[0].values()):
        assert "Hunter2!xyz-fake" not in text, text
    assert "example-admin" in ex.content


def test_an_extraction_without_a_credential_is_untouched():
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import scrub_credential_extraction

    ex = Extraction(content="The lab router lives at 203.0.113.10 on the bench network",
                    extraction_type="entity", confidence=0.9)
    assert scrub_credential_extraction(ex) is False
    assert ex.content == "The lab router lives at 203.0.113.10 on the bench network"


def test_a_tag_loses_the_value_even_inside_a_longer_word():
    """Prose keeps word boundaries; a tag does not need them (a live tag carried
    its password that way)."""
    held, _, tags, _ = ro.split_secret(
        _body(tags=[f"pw{_PASSWORD}x"]), json.dumps([f"pw{_PASSWORD}x"]),
    )
    assert _PASSWORD not in tags
    assert _PASSWORD not in held


# ─── review round 1 (#3898) ──────────────────────────────────────────────────


async def test_the_concept_loses_the_value_too(db):
    """An extraction with no entities derives the identifier from its own text,
    which can carry the password; the concept is indexed in knowledge_fts."""
    store = _store()
    unit_id = await ingest_knowledge_unit(
        store=store, db=db, content=_body(), project=ro.REFERENCE_PROJECT,
        domain="reference.credentials", concept=f"router login {_PASSWORD}",
    )
    row = await kc.get(db, unit_id)
    fts = await db.execute_fetchall("SELECT concept FROM knowledge_fts WHERE unit_id = ?", (unit_id,))
    assert _PASSWORD not in row["concept"] and _PASSWORD not in fts[0][0]
    assert await _secret(db, unit_id) == _VALUE


async def test_the_db_layer_refuses_a_second_value_line_after_the_placeholder(db):
    body = f"[reference.credentials] X\n\nd\n\nValue: {kc.SECRET_PLACEHOLDER}\n\nValue: {_PASSWORD}"
    with pytest.raises(ValueError, match="carries its value"):
        await kc.upsert(db, project_type="reference", domain="reference.credentials",
                        source_doc="x", concept="c", body=body)
    body = f"[reference.credentials] X\n\nd\n\nValue: {kc.SECRET_PLACEHOLDER}\n{_PASSWORD}"
    with pytest.raises(ValueError, match="carries its value"):
        await kc.upsert(db, project_type="reference", domain="reference.credentials",
                        source_doc="x", concept="c", body=body)


def test_a_description_carrying_the_value_marker_is_refused():
    with pytest.raises(ValueError, match="exactly one"):
        ro.split_secret(_body(description="note\n\nValue: tail-fake"))


async def test_a_new_credential_with_no_value_to_hold_is_refused(db):
    """A held-out body under a NEW identifier (or a value equal to the
    placeholder) brings no secret: refused, nothing written."""
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")
    with pytest.raises(ValueError, match="no value to hold"):
        await ingest_knowledge_unit(
            store=_store(), db=db, content=held, project=ro.REFERENCE_PROJECT,
            domain="reference.credentials", concept="brand new",
        )
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_units") == []


async def test_the_secret_lands_on_the_row_that_survives_the_key_conflict(db):
    """A caller passing a fresh id for an existing key: the row keeps its
    original id, and the secret must be written under THAT id."""
    first = await _ingest(db, _store(), _body())
    actual, inserted = await kc.upsert(
        db, id="some-other-id", project_type="reference", domain="reference.credentials",
        source_doc="x", concept="Lab router admin",
        body=(await kc.get(db, first))["body"], secret_value="user: example-admin, password: New!Fake-3",
    )
    assert actual == first and inserted is False
    assert await _secret(db, first) == "user: example-admin, password: New!Fake-3"
    assert await _secret(db, "some-other-id") is None


async def test_a_refused_write_leaves_nothing_for_another_commit(db):
    held = _body().replace(f"Value: {_VALUE}", f"Value: {kc.SECRET_PLACEHOLDER}")
    with pytest.raises(ValueError):
        await kc.upsert(db, project_type="reference", domain="reference.credentials",
                        source_doc="x", concept="new", body=held, _commit=False)
    await db.commit()
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_units") == []
    assert await db.execute_fetchall("SELECT 1 FROM knowledge_fts") == []


def test_every_password_in_one_extraction_is_scrubbed():
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import scrub_credential_extraction

    ex = Extraction(
        content="Lab router login: username: admin, password: First!Fake9; backup password: Second!Fake8",
        extraction_type="entity", confidence=0.9,
        event_verb="set password: First!Fake9", temporal="Second!Fake8",
    )
    assert scrub_credential_extraction(ex) is True
    for text in (ex.content, ex.event_verb, ex.temporal):
        assert "First!Fake9" not in text and "Second!Fake8" not in text, text


def test_glued_short_and_multiword_secrets_are_all_held():
    assert "xy" in ro.secret_fragments("operator / xy")
    assert ro.scrub("prefixHunter2!xyzsuffix", ["Hunter2!xyz"]) == f"prefix{ro.REDACTED}suffix"
    frags = ro.secret_fragments("password: blue moon river")
    assert {"blue", "moon", "river"} <= set(frags)
    held, _, _, _ = ro.split_secret(_body(value="password: blue moon river",
                                          description="the blue moon one"))
    assert "blue moon" not in held


async def test_a_point_another_unit_still_names_is_not_deleted(db):
    store = _store()
    a = await ingest_knowledge_unit(store=store, db=db, content="shared text", project="p",
                                    domain="d", concept="a")
    await ingest_knowledge_unit(store=store, db=db, content="shared text", project="p",
                                domain="d", concept="b")
    store.store = AsyncMock(return_value="qid-2")
    await ingest_knowledge_unit(store=store, db=db, content="changed", project="p",
                                domain="d", concept="a")
    store.delete.assert_not_awaited()
    assert (await kc.get(db, a))["qdrant_id"] == "qid-2"


def test_distillation_and_uploads_refuse_credential_units_before_any_vector():
    import inspect

    from genesis.knowledge import ingest_upload, orchestrator

    for module, before in ((orchestrator, "qdrant_ids: list[str] = []"),
                           (ingest_upload, "qdrant_id = await memory_mod._store.store(")):
        src = inspect.getsource(module)
        assert src.index("holds_secret(") < src.index(before), module.__name__


def test_a_labelled_all_letter_password_is_redacted_even_glued():
    """A labelled secret IS the secret: word boundaries protect prose words
    ("inactive"), never a password run into other text."""
    held, _, _, _ = ro.split_secret(_body(value="password: sunshinefake",
                                          description="old: xsunshinefakes"))
    assert "sunshinefake" not in held
    assert "inactive" in ro.scrub("inactive", ["active"])
