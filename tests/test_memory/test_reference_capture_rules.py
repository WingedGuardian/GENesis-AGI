"""Auto-capture rules for credentials found in conversation (#2426, part 1b).

The extraction job's classifier and ``captured_secrets`` share one capture
rule; a connection string with a userinfo password is kept as a credential; a
credential shape never falls through to the url or network kinds; capture is
insert-only and keys on a redacted identifier with a random tag, never on the
value.

Fake credentials and TEST-NET (203.0.113.x) values only — never real secrets.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.memory import reference_ops as ro
from genesis.memory.knowledge_ingest import ingest_knowledge_unit

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


@pytest.mark.parametrize("phrase", ["password is sunshinefake", "pin is 48151623"])
def test_an_unshaped_password_never_falls_through_to_a_weaker_kind(phrase):
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    content = f"Forum at https://forum.example.org for the bot, username bob, {phrase}"
    ex = Extraction(content=content, extraction_type="entity", confidence=0.9)
    ref = classify_as_reference(ex, user_text="https://forum.example.org")
    assert ref is None or ref["kind"] == "credentials"


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
        ex = Extraction(
            content=f"Forum bot password: {password} for the weekly job",
            extraction_type="entity",
            confidence=0.9,
        )
        units.append(
            await ingest_reference_from_extraction(
                ex,
                store=_store(),
                db=db,
                source_session_id="s",
            )
        )
    assert units[0] != units[1]
    assert {await _secret(db, u) for u in units} == {"password: Old!Fake1x", "password: New!Fake2y"}


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


def _patched_tags(monkeypatch, tags: list[str]) -> None:
    import genesis.memory.reference_extraction as rx

    it = iter(tags)
    monkeypatch.setattr(rx, "secrets_token_hex", lambda _n: next(it))


def _capture(password: str):
    from genesis.memory.extraction import Extraction

    return Extraction(
        content=f"Forum bot password: {password} for the weekly job",
        extraction_type="entity",
        confidence=0.9,
    )


async def test_two_concurrent_captures_on_one_key_both_persist(db, monkeypatch):
    """4484515141: two captures that land on the SAME key (their random tags
    collide here by construction) with different values. Both persist, the
    second under a fresh tag; neither replaces the other."""

    from genesis.memory.reference_extraction import ingest_reference_from_extraction

    _patched_tags(monkeypatch, ["aaaaaa", "aaaaaa", "bbbbbb", "cccccc"])
    units = await asyncio.gather(
        *(
            ingest_reference_from_extraction(
                _capture(pw),
                store=_store(),
                db=db,
                source_session_id="s",
                user_text=None,
            )
            for pw in ("Old!Fake1x", "New!Fake2y")
        )
    )
    assert None not in units and units[0] != units[1]
    assert {await _secret(db, u) for u in units} == {"password: Old!Fake1x", "password: New!Fake2y"}
    concepts = {(await kc.get(db, u))["concept"].rsplit(" #", 1)[1] for u in units}
    assert "aaaaaa" in concepts and len(concepts) == 2


async def test_the_same_capture_twice_on_one_key_is_one_row(db, monkeypatch):
    from genesis.memory.reference_extraction import ingest_reference_from_extraction

    _patched_tags(monkeypatch, ["aaaaaa", "aaaaaa"])
    store = _store()
    units = [
        await ingest_reference_from_extraction(
            _capture("Old!Fake1x"),
            store=store,
            db=db,
            source_session_id="s",
        )
        for _ in range(2)
    ]
    assert units[0] == units[1]
    assert (await db.execute_fetchall("SELECT count(*) FROM knowledge_units"))[0][0] == 1
    assert await _secret_rows(db) == 1
    assert store.store.await_count == 1  # the no-op stored no second vector


async def test_a_recapture_of_a_value_under_a_tagged_key_adds_no_copy(db, monkeypatch):
    """Review SF-1: a value already saved under a tagged form of the name is a
    no-op, not a fresh tag on every later extraction that mentions it."""
    from genesis.memory.reference_extraction import ingest_reference_from_extraction

    _patched_tags(monkeypatch, ["aaaaaa", "bbbbbb", "cccccc", "dddddd"])
    first = await ingest_reference_from_extraction(
        _capture("Old!Fake1x"),
        store=_store(),
        db=db,
        source_session_id="s",
    )
    second = await ingest_reference_from_extraction(
        _capture("New!Fake2y"),
        store=_store(),
        db=db,
        source_session_id="s",
    )
    assert first != second
    again = await ingest_reference_from_extraction(
        _capture("New!Fake2y"),
        store=_store(),
        db=db,
        source_session_id="s",
    )
    assert again == second
    assert (await db.execute_fetchall("SELECT count(*) FROM knowledge_units"))[0][0] == 2
    assert await _secret_rows(db) == 2


async def test_a_recapture_of_a_legacy_row_value_adds_no_copy(db, monkeypatch):
    """Review SF-2: a credential written before #2426 (value inline, no
    secret_ref) holding the captured value is a no-op too."""
    import genesis.memory.reference_extraction as rx

    _patched_tags(monkeypatch, ["aaaaaa", "bbbbbb", "cccccc"])
    probe = await rx.ingest_reference_from_extraction(
        _capture("Old!Fake1x"),
        store=_store(),
        db=db,
        source_session_id="s",
    )
    row = await kc.get(db, probe)
    value = await _secret(db, probe)
    base = rx._TAG_SUFFIX.sub("", row["concept"])
    await kc.delete(db, probe)
    await db.execute("DROP TRIGGER knowledge_units_credential_body_insert")
    await db.execute("DROP TRIGGER knowledge_units_secret_ref_insert")
    legacy_body = rx._format_reference_body(
        kind="credentials",
        identifier=base,
        description="d",
        value=value,
        tags=[],
        session_id="s",
    )
    await db.execute(
        "INSERT INTO knowledge_units (id, project_type, domain, source_doc, concept, body, "
        "ingested_at) VALUES ('legacy', 'reference', 'reference.credentials', 'x', ?, ?, 'now')",
        (base, legacy_body),
    )
    for ddl in kc.secret_ref_trigger_ddl() + kc.credential_body_trigger_ddl():
        await db.execute(ddl)
    again = await rx.ingest_reference_from_extraction(
        _capture("Old!Fake1x"),
        store=_store(),
        db=db,
        source_session_id="s",
    )
    assert again == "legacy"
    assert (await db.execute_fetchall("SELECT count(*) FROM knowledge_units"))[0][0] == 1


# ─── the identifier the write accepts (#2426 part 1b) ───────────────────────


def test_the_login_is_named_in_the_identifier_before_its_tag():
    from genesis.memory.reference_extraction import _with_login

    assert _with_login("Lab router", "admin7", "First!Fake9") == "Lab router (admin7)"
    assert (
        _with_login("Lab router #a1b2c3", "admin7", "First!Fake9") == "Lab router (admin7) #a1b2c3"
    )
    # Already named, or no login: unchanged.
    assert (
        _with_login("admin7 on the lab router", "admin7", "First!Fake9")
        == "admin7 on the lab router"
    )
    assert _with_login("API key", None, "sk-fake-Zq81Lm") == "API key"


def test_an_identifier_still_carrying_the_value_falls_back_and_keeps_the_login():
    from genesis.memory.reference_extraction import _identifier_the_write_accepts

    login = {"login": "admin7", "secret": "First!Fake9"}
    kept = _identifier_the_write_accepts("Lab router (admin7)", "admin7 / First!Fake9", **login)
    assert kept == "Lab router (admin7)"
    fallback = _identifier_the_write_accepts(
        "router First!Fake9 (admin7)", "admin7 / First!Fake9", **login
    )
    assert "First!Fake9" not in fallback
    assert fallback.startswith("credential (admin7) #")


async def test_a_capture_whose_text_glues_the_secret_into_its_name_is_stored(db):
    """The capture rule cannot isolate a secret glued into the entity name in
    another case (its redaction is exact); the write's field check would refuse
    the identifier and the capture would be lost. It is stored under the
    fallback name instead."""
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import ingest_reference_from_extraction

    ex = Extraction(
        content="Lab router login: username: admin7, password: Glued!Fake42",
        extraction_type="entity",
        confidence=0.9,
        entities=["routerGLUED!FAKE42"],
    )
    unit = await ingest_reference_from_extraction(ex, store=_store(), db=db, source_session_id="s")
    assert unit is not None
    row = await kc.get(db, unit)
    for text in (row["body"].casefold(), row["concept"].casefold()):
        assert "glued!fake42" not in text
    assert row["concept"].startswith("credential (admin7) #")
    assert await _secret(db, unit) == "admin7 / Glued!Fake42"


# ─── security review fixes (#2426 part 1b) ──────────────────────────────────


async def _indexed_text(db) -> str:
    """Every text value of every knowledge_units and knowledge_fts row, joined:
    what the identifier, the body, the tags and the full-text index hold."""
    out: list[str] = []
    for table in ("knowledge_units", "knowledge_fts"):
        for row in await db.execute_fetchall(f"SELECT * FROM {table}"):  # noqa: S608 - fixed names
            out.extend(v for v in tuple(row) if isinstance(v, str))
    return "\n".join(out)


async def _capture_text(db, content: str, entities: list[str] | None = None):
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import ingest_reference_from_extraction

    ex = Extraction(
        content=content, extraction_type="entity", confidence=0.9, entities=entities or [],
    )
    return await ingest_reference_from_extraction(ex, store=_store(), db=db, source_session_id="s")


_FAKE_PAT = "ghp_" + "Fk9" * 13 + "x"  # 44 characters, fake


@pytest.mark.parametrize(
    "content, entities, secret, value, concept",
    [
        # HIGH-1: a login equal to its password is never named.
        (
            "Lab router login: username: abc123xy, password: abc123xy",
            ["Lab router"], "abc123xy", "abc123xy / abc123xy", "Lab router",
        ),
        # The same with no entity: the redacted text is not un-redacted.
        (
            "username: abc123xy, password: abc123xy", [], "abc123xy", "abc123xy / abc123xy", None,
        ),
        # A token used as the username is never named.
        (
            f"Git login: username: {_FAKE_PAT}, password: x-oauth-basic",
            ["Git"], _FAKE_PAT, f"{_FAKE_PAT} / x-oauth-basic", "Git",
        ),
        # A plain-word password equal to the login: not named either.
        (
            "Lab router login: username: admin, password: admin",
            ["Lab router"], "admin", "admin / admin", "Lab router",
        ),
    ],
)
async def test_a_login_that_is_or_holds_the_secret_is_never_named(
    db, content, entities, secret, value, concept,
):
    unit = await _capture_text(db, content, entities)
    assert unit is not None
    row = await kc.get(db, unit)
    if concept is not None:
        assert row["concept"] == concept
    assert secret.casefold() not in row["concept"].casefold()
    if secret != "admin":  # a plain word is matched whole, as the store does
        assert secret not in await _indexed_text(db)
    assert await _secret(db, unit) == value


async def test_a_second_secret_in_an_entity_tag_is_dropped(db):
    """HIGH-2: the classifier picks the first password; the backup one, also
    named as an entity, never reaches the tags, the body trailer or the FTS."""
    unit = await _capture_text(
        db,
        "Router username: admin7, password: First!Fake9; backup password: Second!Fake8xx",
        ["Router", "Second!Fake8xx"],
    )
    assert unit is not None
    indexed = await _indexed_text(db)
    assert "Second!Fake8xx" not in indexed and "First!Fake9" not in indexed
    assert "Router" in json.loads((await kc.get(db, unit))["tags"])
    assert await _secret(db, unit) == "admin7 / First!Fake9"


async def test_a_second_secret_in_the_entity_identifier_is_redacted(db):
    """HIGH-2: the entity branch of the identifier is redacted like the text."""
    unit = await _capture_text(
        db,
        "Router username: admin7, password: First!Fake9; backup password: Second!Fake8xx",
        ["Backup Second!Fake8xx"],
    )
    assert unit is not None
    row = await kc.get(db, unit)
    assert row["concept"].startswith("Backup [redacted] (admin7) #")
    assert "Second!Fake8xx" not in await _indexed_text(db)
    assert await _secret(db, unit) == "admin7 / First!Fake9"


def test_the_identifier_check_probes_every_captured_secret():
    """HIGH-2: the fallback check refuses an identifier carrying a secret other
    than the stored value."""
    from genesis.memory.reference_extraction import _identifier_the_write_accepts

    out = _identifier_the_write_accepts(
        "Backup Second!Fake8xx", "admin7 / First!Fake9",
        login="admin7", secret="First!Fake9", secrets={"First!Fake9", "Second!Fake8xx"},
    )
    assert "Second!Fake8xx" not in out
    assert out.startswith("credential (admin7) #")


@pytest.mark.parametrize(
    "content, secret",
    [
        # MEDIUM-1a: an unencoded "/" in a userinfo password.
        (
            "Staging endpoint https://deploy:Pa/ss1234@ci.example.com/job for the nightly build",
            "Pa/ss1234",
        ),
        # MEDIUM-1b: a credential-named query parameter.
        (
            "Deploy webhook endpoint https://ci.example.com/hook?token=abcd1234efgh5678 for jobs",
            "abcd1234efgh5678",
        ),
        (
            "Signed download link https://files.example.org/a.zip?X-Amz-Signature=Fk9sig0000ab",
            "Fk9sig0000ab",
        ),
        # Second review: any name, any spelling, any parameter position. A value
        # that mixes letters and digits is judged by its shape alone.
        ("Session link https://app.example.org/x?sessionid=s3ss10nFake88 for support", "s3ss10nFake88"),
        ("OAuth return https://app.example.org/cb?code=Ab12Cd34Ef56 after login", "Ab12Cd34Ef56"),
        ("Embed https://app.example.org/e?jwt=eyJfake.p4yload.s1g for the board", "eyJfake.p4yload.s1g"),
        ("Link https://app.example.org/x?\uff54\uff4f\uff4b\uff45\uff4e=Fullw1dthTok for it", "Fullw1dthTok"),
        ("Link https://app.example.org/x?to\u200bken=Zer0w1dthTok for it", "Zer0w1dthTok"),
        ("Link https://app.example.org/x?t%6Fken=Enc0dedNameTok for it", "Enc0dedNameTok"),
        ("Link https://app.example.org/x#token=Fragm3ntTok99 for it", "Fragm3ntTok99"),
        ("Link https://app.example.org/x;token=Matr1xTok99a for it", "Matr1xTok99a"),
        ("Link https://app.example.org/x?token=Enc%6FdedVal99 for it", "Enc%6FdedVal99"),
    ],
)
async def test_a_url_carrying_a_secret_the_userinfo_rule_misses_is_not_stored(db, content, secret):
    assert await _capture_text(db, content, ["CI"]) is None
    assert secret not in await _indexed_text(db)
    assert (await db.execute_fetchall("SELECT count(*) FROM knowledge_units"))[0][0] == 0


def test_an_ordinary_url_is_still_a_url_reference():
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import classify_as_reference

    content = "The team wiki lives at https://wiki.example.org/home?page=intro for onboarding"
    ref = classify_as_reference(Extraction(content=content, extraction_type="entity", confidence=0.9))
    assert ref is not None and ref["kind"] == "url"


@pytest.mark.parametrize(
    "url",
    [
        "https://wiki.example.org/list?sort_key=created_at_desc&page=2",
        "https://wiki.example.org/list?order_by=last_name&id=12345678",
        "https://wiki.example.org/docs#getting-started",
    ],
)
def test_plain_url_parameters_are_not_read_as_secrets(url):
    """A parameter value with no digit, or no letter, is not secret-shaped, so
    an ordinary URL is still stored as a url reference."""
    from genesis.memory.extraction import Extraction
    from genesis.memory.reference_extraction import captured_secrets, classify_as_reference

    assert captured_secrets([url]) == set()
    content = f"The team wiki list is at {url} for onboarding"
    ref = classify_as_reference(Extraction(content=content, extraction_type="entity", confidence=0.9))
    assert ref is not None and ref["kind"] == "url"


async def test_a_quoted_env_value_is_stored_whole(db):
    """MEDIUM-2: the env branch stores the value the capture rule isolates."""
    unit = await _capture_text(
        db, 'Backups env file: DB_PASSWORD="Fake9pw horse battery" for the nightly job', ["Backups"],
    )
    assert unit is not None
    indexed = await _indexed_text(db)
    assert "Fake9pw" not in indexed and "horse battery" not in indexed
    assert await _secret(db, unit) == "DB_PASSWORD=Fake9pw horse battery"


async def test_a_quoted_password_holding_a_slash_separator_is_kept(db):
    """MEDIUM-3: a "password: ..." value has no login, so no part of it is
    named as one and the capture is not lost."""
    unit = await _capture_text(db, 'Vault password: "Tr0ub4dor / hunter2zz" for the backups')
    assert unit is not None
    row = await kc.get(db, unit)
    assert "(" not in row["concept"]
    indexed = await _indexed_text(db)
    assert "Tr0ub4dor" not in indexed and "hunter2zz" not in indexed
    assert await _secret(db, unit) == "password: Tr0ub4dor / hunter2zz"


@pytest.mark.parametrize(
    "login, secret, named",
    [
        ("admin7", "First!Fake9", True),
        ("admin", "admin", False),
        ("abc123xy", "abc123xy", False),
        ("admin", "admin123x", False),  # the login is inside the secret
        ("admin123x", "admin", False),  # the secret is inside the login
        (_FAKE_PAT, "x-oauth-basic", False),
        ("sk-" + "Fk9" * 8, "x", False),
        ("Password: Tr0ub4dor", "hunter2zz", False),  # captured_secrets sees it
    ],
)
def test_a_login_is_named_only_when_it_is_no_part_of_a_secret(login, secret, named):
    from genesis.memory.reference_extraction import _with_login

    assert (_with_login("Lab", login, secret) != "Lab") is named
