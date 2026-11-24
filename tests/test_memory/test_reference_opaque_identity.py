"""A credential's identity is an opaque key plus the hash of its full name (#2426).

The key (``cred-`` and 12 hex) is minted once and never spells a name or a
value; a write by name finds the entry by the hash of its normalised name, and
a write by key goes only through ``entry_key``. Fake credentials and
example.org names only.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.mcp.memory.knowledge import _format_reference_body as fmt
from genesis.memory import reference_ops as ro
from genesis.memory.knowledge_ingest import UnknownEntryKey, ingest_untrusted_credential
from tests.test_memory.test_reference_secret_class_fixes import _ingest, _PointStore

MIG = importlib.import_module("genesis.db.migrations.20261115060803_credential_name_hash")
_OPAQUE = re.compile(r"cred-[0-9a-f]{12}")


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


class _SlowStore(_PointStore):
    """Yields to the loop while storing, so concurrent writes interleave
    between their read of the name and their unit write."""

    async def store(self, content, source, **kwargs):
        await asyncio.sleep(0)
        return await super().store(content, source, **kwargs)


def _body(identifier: str, value: str) -> str:
    return fmt(
        kind="credentials",
        identifier=identifier,
        description="lab login",
        value=value,
        tags=None,
        source=None,
    )


async def _save(db, store, name, value, **kw):
    return await _ingest(db, store, _body(name or "ignored", value), concept=name, **kw)


async def _units(db) -> int:
    return len(await db.execute_fetchall("SELECT 1 FROM knowledge_units"))


async def _concepts(db) -> list[str]:
    rows = await db.execute_fetchall("SELECT concept FROM knowledge_units")
    return [r[0] for r in rows] + [
        r[0] for r in await db.execute_fetchall("SELECT concept FROM knowledge_fts")
    ]


async def _legacy(db, key: str, value: str) -> str:
    """A credential stored before opaque keys: its key is its name, no hash."""
    unit, _ = await kc.upsert(
        db,
        project_type="reference",
        domain="reference.credentials",
        source_doc="x",
        concept=key,
        body=f"[reference.credentials] {key}\n\n{ro.DETAIL_PLACEHOLDER}\n\nValue: {ro.SECRET_PLACEHOLDER}",
        secret_value=value,
        secret_detail="",
    )
    return unit


async def test_three_rotations_by_name_and_by_key_stay_one_named_entry(db):
    store = _PointStore()
    name = "Lab Router admin"
    values = ["Aa11Bb22!x", "Cc33Dd44!y", "Ee55Ff66!z", "Gg77Hh88!w"]
    first = await _save(db, store, name, values[0])
    key = (await kc.get(db, first))["concept"]
    assert _OPAQUE.fullmatch(key)
    assert await _save(db, store, name, values[1]) == first
    assert await _save(db, store, None, values[2], entry_key=key) == first
    assert await _save(db, store, "lab  ROUTER admin", values[3]) == first
    assert await _units(db) == 1
    row = await kc.get(db, first)
    assert row["concept"] == key and ro.display_name(row) == name
    assert row["body"].startswith(f"[reference.credentials] {name}\n")
    assert await kc.get_reference_secret(db, first) == values[3]
    for concept in await _concepts(db):
        assert not any(v in concept for v in values), concept


async def test_a_long_name_rotated_by_key_then_saved_by_name_is_one_entry(db):
    """Round-4 comment 4576838397's shape: a 600-character name rotated through
    its key, then saved under the original name, updates the same entry."""
    store = _PointStore()
    name = "db admin " + "x" * 600
    first = await _save(db, store, name, "Hunter2Pass9!")
    key = (await kc.get(db, first))["concept"]
    await _save(db, store, None, "Other8Pass7!", entry_key=key)
    assert await _save(db, store, name, "Third9Pass6!") == first
    assert await _units(db) == 1
    # The searchable name is capped; the full name stays recorded in meta.
    row = await kc.get(db, first)
    assert name.startswith(ro.display_name(row)) and len(ro.display_name(row)) <= 512
    assert (await ro.reveal_meta(db, row))["identifier"] == name
    assert await kc.get_reference_secret(db, first) == "Third9Pass6!"


@pytest.mark.parametrize("writers", [3, 6])
async def test_concurrent_first_saves_of_a_name_give_one_entry(db, writers):
    store = _SlowStore()
    units = await asyncio.gather(
        *(_save(db, store, "Lab VPN login", f"Pw{i}fake!x") for i in range(writers))
    )
    assert len(set(units)) == 1 and await _units(db) == 1


@pytest.mark.parametrize("backfilled", [False, True], ids=["fallback", "backfill"])
async def test_a_legacy_plain_key_is_found_by_its_name(db, backfilled):
    store = _PointStore()
    legacy = await _legacy(db, "lab wifi", "Old1Pass!x")
    if backfilled:
        await MIG.up(db)
        rows = await db.execute_fetchall("SELECT name_hash FROM knowledge_units")
        assert rows[0][0] == ro.name_hash("lab wifi")
    unit = await _save(db, store, "Lab WiFi", "New2Pass!y")
    assert unit == legacy and await _units(db) == 1
    rows = await db.execute_fetchall("SELECT concept, name_hash, name_origin FROM knowledge_units")
    assert [tuple(r) for r in rows] == [("lab wifi", ro.name_hash("lab wifi"), "trusted")]


async def test_legacy_names_differing_only_in_case_keep_both_and_the_earliest_wins(db):
    store = _PointStore()
    early = await _legacy(db, "Lab WiFi", "Early1Pass!x")
    late = await _legacy(db, "lab wifi", "Late2Pass!y")
    await MIG.up(db)  # builds the unique index over the earliest only
    index = await db.execute_fetchall(
        "SELECT 1 FROM sqlite_master WHERE name = 'idx_knowledge_units_name_hash'"
    )
    assert index
    assert await _save(db, store, "LAB WIFI", "New3Pass!z") == early
    assert await _units(db) == 2
    for unit, value in ((early, "New3Pass!z"), (late, "Late2Pass!y")):
        assert (await ro.reveal_reference(db, await kc.get(db, unit)))[0] == value
    assert await ro.delete_reference_entry(db, None, late)
    assert await _units(db) == 1


async def test_text_shaped_like_a_key_is_a_name(db):
    store = _PointStore()
    first = await _save(db, store, "lab wifi", "Pw1fake!a")
    key = (await kc.get(db, first))["concept"]
    other = await _save(db, store, key, "Pw2fake!b")
    assert other != first and await kc.get_reference_secret(db, first) == "Pw1fake!a"
    row = await kc.get(db, other)
    assert row["concept"] != key and ro.display_name(row) == key


async def test_an_unknown_entry_key_is_refused_with_nothing_written(db):
    store = _PointStore()
    with pytest.raises(UnknownEntryKey):
        await _save(db, store, "lab wifi", "Pw1fake!a", entry_key="cred-000000000000")
    assert await _units(db) == 0 and store.points == {}
    with pytest.raises(ValueError, match="entry_key"):
        await _ingest(db, store, "a url", domain="reference.url", concept="x", entry_key="k")


async def test_an_insert_only_write_never_updates_a_trusted_entry(db):
    store = _PointStore()
    trusted = await _save(db, store, "lab wifi", "Pw1fake!a")
    same = await _save(db, store, "lab wifi", "Pw1fake!a", insert_only=True)
    assert same != trusted and await kc.get_reference_secret(db, trusted) == "Pw1fake!a"
    # The same credential from an untrusted source again is that untrusted entry.
    assert await _save(db, store, "lab wifi", "Pw1fake!a", insert_only=True) == same
    other = await _save(db, store, "lab wifi", "Pw2fake!b", insert_only=True)
    assert other not in (trusted, same) and await _units(db) == 3
    origins = await db.execute_fetchall("SELECT name_origin FROM knowledge_units ORDER BY rowid")
    assert [r[0] for r in origins] == ["trusted", "untrusted", "untrusted"]
    # A trusted save by name still updates the trusted entry.
    assert await _save(db, store, "lab wifi", "Pw3fake!c") == trusted


async def test_the_views_show_the_name(db, monkeypatch):
    import genesis.mcp.memory_mcp as memory_mod
    from genesis.dashboard.routes.references import _summary
    from genesis.mcp.memory.knowledge import reference_lookup, reference_store

    store = _PointStore()
    monkeypatch.setattr(memory_mod, "_require_init", lambda: None)
    monkeypatch.setattr(memory_mod, "_store", store)
    monkeypatch.setattr(memory_mod, "_db", db)
    monkeypatch.setattr(memory_mod, "_qdrant", MagicMock())
    monkeypatch.setattr(memory_mod, "_retriever", MagicMock(recall=AsyncMock(return_value=[])))
    fn = getattr(reference_store, "fn", reference_store)
    unit = await fn(kind="credentials", identifier="lab wifi", value="Pw1fake!a", description="lab")
    row = await kc.get(db, unit)
    assert row["concept"] not in unit
    summary = _summary(row)
    assert summary["name"] == "lab wifi" and summary["concept"] == row["concept"]
    lookup = getattr(reference_lookup, "fn", reference_lookup)
    hit = next(r for r in await lookup(query="wifi", kind="credentials") if r["unit_id"] == unit)
    assert hit["name"] == "lab wifi" and hit["concept"] == row["concept"]
    again = await fn(
        kind="credentials",
        identifier="unused",
        value="Pw2fake!b",
        description="lab",
        entry_key=row["concept"],
    )
    # The identifier given with a key is not used: the reply names the entry's.
    assert again == f"{unit} (stored as: lab wifi)"
    assert ro.display_name(await kc.get(db, unit)) == "lab wifi"
    with pytest.raises(ValueError, match="entry_key"):
        await fn(
            kind="url",
            identifier="x",
            value="https://example.org",
            description="d",
            entry_key=row["concept"],
        )


async def test_delete_removes_the_row_with_its_hash(db):
    store = _PointStore()
    unit = await _save(db, store, "lab wifi", "Pw1fake!a")
    assert await ro.delete_reference_entry(db, None, unit)
    assert (
        await db.execute_fetchall("SELECT 1 FROM knowledge_units WHERE name_hash IS NOT NULL") == []
    )
    fresh = await _save(db, store, "lab wifi", "Pw2fake!b")
    assert fresh != unit and await _units(db) == 1


async def test_a_database_without_the_name_columns_still_takes_writes(db):
    """A process on new code may write before the migration adds the columns."""
    await db.execute("DROP INDEX idx_knowledge_units_name_hash")
    await db.execute("ALTER TABLE knowledge_units DROP COLUMN name_hash")
    await db.execute("ALTER TABLE knowledge_units DROP COLUMN name_origin")
    store = _PointStore()
    first = await _save(db, store, "lab wifi", "Pw1fake!a")
    assert await _save(db, store, "Lab  WiFi", "Pw2fake!b") == first
    assert await _units(db) == 1 and await kc.get_reference_secret(db, first) == "Pw2fake!b"


def test_the_name_hash_is_pinned():
    """Golden values: a change to the normalisation would silently re-key
    every stored name, so it must come with its own rehash migration."""
    assert ro.name_hash("lab wifi") == ro.name_hash("  LAB​   WiFi ")
    assert ro.name_hash("lab wifi") == "9fda4fde31e0e86ef45dd6ba89127a45eea7ba9aae0ae5bd9755b3d3614004d4"


async def test_a_name_ending_like_a_tag_names_only_itself(db):
    """A key that merely ends in `` #`` and six hex is a name like any other:
    saving its prefix never reaches it."""
    store = _PointStore()
    legacy = await _legacy(db, "Locker #123456", "Ll11Mm22!q")
    other = await _save(db, store, "Locker", "Nn33Oo44!r")
    assert other != legacy
    assert await ro.resolve_value(db, await kc.get(db, legacy)) == "Ll11Mm22!q"
    assert await _save(db, store, "Locker #123456", "Pp55Qq66!s") == legacy
    assert await _units(db) == 2


async def test_an_identifier_with_a_line_break_carrying_its_value_is_masked(db):
    """The identifier is masked as given, not only flattened: a line-broken
    value in it is held, and the write lands."""
    store = _PointStore()
    secret = "correct horse\nbattery staple"
    unit = await _save(db, store, secret, secret)
    assert await _units(db) == 1 and await kc.get_reference_secret(db, unit) == secret
    for text in await _indexed(db, store):
        assert "correct horse" not in text and "battery staple" not in text, text


async def test_a_body_without_the_standard_header_still_names_its_entry(db):
    """Codex round 1 on #4749: a body whose first line is not the standard
    header gains it, so the name is searchable and shown, and the caller's own
    first line is kept (held with the description, never dropped)."""
    store = _PointStore()
    unit = await _ingest(
        db, store, "VPN account\n\nLab account\n\nValue: Rr77Ss88!t", concept="Lab VPN login",
    )
    row = await kc.get(db, unit)
    assert _OPAQUE.fullmatch(row["concept"])
    assert row["body"].split("\n", 1)[0] == "[reference.credentials] Lab VPN login"
    assert ro.display_name(row) == "Lab VPN login"
    fts = await db.execute_fetchall("SELECT body FROM knowledge_fts WHERE rowid = (SELECT rowid FROM knowledge_units WHERE id = ?)", (unit,))
    assert not fts or "Lab VPN login" in fts[0][0]
    assert "Rr77Ss88" not in row["body"]
    composed, missing = await ro.compose_body(db, row)
    assert not missing and "VPN account" in composed and "Rr77Ss88!t" in composed
    assert await _save(db, store, "lab vpn LOGIN", "Uu99Vv00!w") == unit


# ─── Masking on opaque identity (#4416's mechanism) ─────────────────────────


async def _held_save(db, store, name: str, value: str, **kw):
    return await _ingest(
        db, store, _body(name, value), concept=name, held_value=value, held_detail="lab login", **kw
    )


async def _untrusted(db, store, name: str, content: str, **kw):
    return await ingest_untrusted_credential(
        store=store,
        db=db,
        content=content,
        project="reference",
        domain="reference.credentials",
        concept=name,
        collection="episodic_memory",
        memory_type="episodic",
        **kw,
    )


async def _indexed(db, store) -> list[str]:
    texts = [
        str(v)
        for table in ("knowledge_units", "knowledge_fts")
        for r in await db.execute_fetchall(f"SELECT concept, body, tags FROM {table}")  # noqa: S608
        for v in r
    ]
    return texts + list(store.points.values())


@pytest.mark.parametrize("hashed", [True, False], ids=["hash", "no-columns"])
async def test_a_capped_name_rotated_by_key_is_found_again_by_its_name(db, hashed):
    """Codex 4576838397 on masked keys: a name over the cap that also holds its
    value, rotated through its returned key, then saved under the original
    name: one entry, its full name kept in meta, and no value is indexed. With
    no hash column the entry is found by the full name its meta records, never
    by its masked, capped header."""
    if not hashed:
        await db.execute("DROP INDEX idx_knowledge_units_name_hash")
        await db.execute("ALTER TABLE knowledge_units DROP COLUMN name_hash")
        await db.execute("ALTER TABLE knowledge_units DROP COLUMN name_origin")
    store = _PointStore()
    name = "db-Hunter2Pass9! " + "x" * 600
    first = await _held_save(db, store, name, "Hunter2Pass9!")
    assert first.name.startswith("db-[held] ") and len(first.name) <= 512
    await _held_save(db, store, "unused", "Other8Pass7!", entry_key=first.concept)
    assert (await ro.reveal_meta(db, await kc.get(db, first)))["identifier"] == name
    third = await _held_save(db, store, name, "Third9Pass6!")
    assert third == first and await _units(db) == 1
    assert await kc.get_reference_secret(db, first) == "Third9Pass6!"
    assert (await ro.reveal_meta(db, await kc.get(db, first)))["identifier"] == name
    for text in await _indexed(db, store):
        for value in ("Hunter2Pass9!", "Other8Pass7!", "Third9Pass6!"):
            assert value not in text


async def test_a_prose_upload_under_a_saved_name_masks_the_saved_value(db):
    """Codex 4576838396: a known held value is a probe even when the incoming
    text is prose, so an upload under ``router Sunny-2024`` masks it."""
    store = _PointStore()
    await _held_save(db, store, "router Sunny-2024", "Sunny-2024")
    upload = await _untrusted(db, store, "router Sunny-2024", "password: New2Pass!y")
    assert upload.name == "router [held]"
    for text in await _indexed(db, store):
        assert "Sunny-2024" not in text and "New2Pass!y" not in text
    meta = json.loads(await kc.get_secret_meta(db, (await kc.get(db, upload))["secret_ref"]))
    assert meta[kc.IDENTITY_META]["value_is_prose"] is True


async def test_a_delimited_save_never_masks_an_uploads_prose_words(db):
    """The reviewer's over-masking case: words of another entry's PROSE value
    are not probes for a delimited write's name."""
    store = _PointStore()
    name = "router WPA2-PSK e-mail"
    await _untrusted(
        db, store, name, "The router uses WPA2-PSK; the e-mail goes to the admin. password: Uu77Vv88Ww",
    )
    saved = await _held_save(db, store, name, "Kk55Ll66Mm")
    assert saved.name == name


async def test_a_name_holding_its_own_secret_stays_one_entry_by_name_and_by_key(db):
    store = _PointStore()
    values = ["Aa11Bb22!x", "Cc33Dd44!y", "Ee55Ff66!z", "Gg77Hh88!w"]
    name = f"vpn {values[0]} login"
    first = await _held_save(db, store, name, values[0])
    await _held_save(db, store, name, values[1])
    await _held_save(db, store, "ignored", values[2], entry_key=first.concept)
    last = await _held_save(db, store, name, values[3])
    assert last == first and await _units(db) == 1
    row = await kc.get(db, first)
    assert _OPAQUE.fullmatch(row["concept"]) and ro.display_name(row) == "vpn [held] login"
    # The rotation by key kept the recorded name, in meta and in the header.
    assert (await ro.reveal_meta(db, row))["identifier"] == name
    for text in await _indexed(db, store):
        assert not any(value in text for value in values), text


async def test_an_insert_only_re_upload_returns_its_entry_unless_the_description_differs(db):
    """Uploads are insert-only: the same name, value and description is the
    entry already stored; another description is an entry of its own."""
    store = _PointStore()
    value = "Nn11Aa22Ss"

    async def upload(detail):
        return await _untrusted(db, store, "lab nas", value, held_value=value, held_detail=detail)

    first = await upload("the nas")
    assert await upload("the nas") == first and await _units(db) == 1
    other = await upload("the backup nas")
    assert other != first and await _units(db) == 2
    assert await kc.get_reference_secret(db, first) == value


async def test_a_reply_names_the_masked_name_never_the_key(db, monkeypatch):
    import genesis.mcp.memory_mcp as memory_mod
    from genesis.mcp.memory.knowledge import reference_store

    monkeypatch.setattr(memory_mod, "_require_init", lambda: None)
    monkeypatch.setattr(memory_mod, "_store", _PointStore())
    monkeypatch.setattr(memory_mod, "_db", db)
    monkeypatch.setattr(memory_mod, "_qdrant", MagicMock())
    fn = getattr(reference_store, "fn", reference_store)
    reply = await fn(
        kind="credentials", identifier="wifi Hunter2024x!fake", value="Hunter2024x!fake", description="lab",
    )
    unit, _, stored_as = reply.partition(" (stored as: ")
    row = await kc.get(db, unit)
    assert stored_as == "wifi [held])" and row["concept"] not in reply
    meta = await ro.reveal_meta(db, row)
    assert meta["identifier"] == "wifi Hunter2024x!fake" and kc.IDENTITY_META not in meta
