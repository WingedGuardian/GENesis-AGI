"""The hold-out chokepoint masks, never refuses (#2426, owner ruling 6f73fe4d).

No credential write is refused or degraded to keep a secret out of the index:
every span of the value is masked in each indexed field (``[held]``), the
original value and description are held in ``reference_secrets``, the write goes
on, and the caller learns the key it was stored under. Each test below is one
refusal the functional trade-off audit listed (items 4, 6, 7b, 9, 10, 11, 12,
13): the write now succeeds, no indexed surface carries the secret
(knowledge_units concept, body and tags; knowledge_fts; what the store is
given, which is the vector payload and the memory_fts row), and lookup and
reveal return the full value and description.

Fake credentials, example.org hosts and TEST-NET addresses only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import aiosqlite
import pytest

from genesis.db.crud import knowledge as kc
from genesis.db.schema import create_all_tables
from genesis.mcp.memory.knowledge import _format_reference_body as fmt
from genesis.memory import reference_ops as ro
from tests.test_memory.test_reference_secret_class_fixes import _ingest, _PointStore

_SECRET = "Hunter2024x!fake"
_VALUE = f"password: {_SECRET}"
_DESC = "Guest wifi for the lab, rotated monthly"


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


def _body(
    identifier: str = "lab wifi", value: str = _VALUE, description: str = _DESC, tags=None
) -> str:
    return fmt(
        kind="credentials",
        identifier=identifier,
        description=description,
        value=value,
        tags=tags,
        source=None,
    )


async def _assert_held(db, store, unit, *, secret=_SECRET, value=_VALUE, description=_DESC) -> dict:
    """The write landed; no indexed surface carries ``secret``; the readers
    return the full value and description."""
    row = await kc.get(db, unit)
    assert row is not None
    fts = await db.execute_fetchall(
        "SELECT concept, body, tags FROM knowledge_fts WHERE unit_id = ?",
        (unit,),
    )
    indexed = [row["concept"], row["body"], row["tags"] or "", *fts[0]]
    indexed += [json.dumps(call, default=str) for call in store.calls]
    indexed += [str(v) for k, v in row.items() if k != "secret_ref"]
    for text in indexed:
        assert secret not in text, text
    assert await ro.reveal_reference(db, row) == (value, description)
    composed, missing = await ro.compose_body(db, row)
    assert missing is False and value in composed and description in composed
    return row


# ─── item 4: the identifier, and its body-header twin ────────────────────────


async def test_an_identifier_carrying_the_value_is_masked_under_a_tagged_key(db):
    store = _PointStore()
    unit = await _ingest(db, store, _body(f"wifi {_SECRET}"), concept=f"wifi {_SECRET}")
    row = await _assert_held(db, store, unit)
    assert (
        row["concept"].startswith("wifi [held] #")
        and len(row["concept"]) == len("wifi [held] #") + 6
    )
    assert unit.concept == row["concept"]
    # The header is rebuilt from the masked identifier.
    assert row["body"].startswith(f"[reference.credentials] {row['concept']}\n")


async def test_re_storing_a_masked_identifier_updates_the_same_entry(db):
    """Owner ruling (review L3): the masked identifier is the entry's identity,
    so the same name and value stored twice is one row."""
    store = _PointStore()
    a = await _ingest(db, store, _body(f"wifi {_SECRET}"), concept=f"wifi {_SECRET}")
    b = await _ingest(db, store, _body(f"wifi {_SECRET}", description="rotated"), concept=f"wifi {_SECRET}")
    assert a == b and a.concept == b.concept
    assert len(await db.execute_fetchall("SELECT 1 FROM knowledge_units")) == 1
    await _assert_held(db, store, b, description="rotated")


async def test_a_rotated_value_under_the_same_masked_identifier_replaces_the_old(db):
    store = _PointStore()
    a = await _ingest(db, store, _body("Wifi Hunter2024x", value="Hunter2024x"), concept="Wifi Hunter2024x")
    b = await _ingest(db, store, _body("Wifi Newpw2025y", value="Newpw2025y"), concept="Wifi Newpw2025y")
    assert a == b and a.concept.startswith("Wifi [held] #")
    assert len(await db.execute_fetchall("SELECT 1 FROM knowledge_units")) == 1
    assert [r[0] for r in await db.execute_fetchall("SELECT value FROM reference_secrets")] == ["Newpw2025y"]
    await _assert_held(db, store, b, secret="Newpw2025y", value="Newpw2025y")


async def test_two_masked_identifiers_with_different_bases_stay_two_entries(db):
    store = _PointStore()
    a = await _ingest(db, store, _body(f"Home Wifi {_SECRET}"), concept=f"Home Wifi {_SECRET}")
    b = await _ingest(db, store, _body(f"Office Wifi {_SECRET}"), concept=f"Office Wifi {_SECRET}")
    assert a != b
    assert {a.concept[:-8], b.concept[:-8]} == {"Home Wifi [held]", "Office Wifi [held]"}
    # The tag is not derived from the value.
    assert _SECRET.casefold() not in a.concept.casefold()


async def test_an_unmasked_identifier_keeps_a_stable_key(db):
    store = _PointStore()
    a = await _ingest(db, store, _body(), concept="lab wifi")
    b = await _ingest(db, store, _body(value="password: Rotated9!fake"), concept="lab wifi")
    assert a == b and (await kc.get(db, a))["concept"] == "lab wifi"


async def test_reference_store_reports_the_key_it_stored_under(db, monkeypatch):
    import genesis.mcp.memory_mcp as memory_mod
    from genesis.mcp.memory.knowledge import reference_store

    store = _PointStore()
    monkeypatch.setattr(memory_mod, "_require_init", lambda: None)
    monkeypatch.setattr(memory_mod, "_store", store)
    monkeypatch.setattr(memory_mod, "_db", db)
    monkeypatch.setattr(memory_mod, "_qdrant", MagicMock())
    fn = getattr(reference_store, "fn", reference_store)
    result = await fn(
        kind="credentials", identifier=f"wifi {_SECRET}", value=_VALUE, description=_DESC
    )
    unit, _, stored_as = result.partition(" (stored as: ")
    row = await _assert_held(db, store, unit)
    assert stored_as == row["concept"] + ")"
    # An identifier carrying nothing of the value: the plain unit id, as before.
    plain = await fn(kind="credentials", identifier="lab wifi", value=_VALUE, description=_DESC)
    assert (await kc.get(db, plain))["concept"] == "lab wifi"


# ─── item 6: caller fields ───────────────────────────────────────────────────


async def test_caller_fields_are_held_with_the_value_and_revealed(db):
    """Owner ruling c91c5b74 (round-3 M-1): a credential's caller metadata is
    held in its secret row verbatim, never masked; the unit carries neutral
    values, and reveal returns the metadata with the value."""
    store = _PointStore()
    provenance = {
        "source_doc": f"note {_SECRET}",
        "extra": {"k": [_SECRET, 7]},
        "section_title": "Ｈｕｎｔｅｒ２０２４ｘ！ｆａｋｅ",
        "session_id": f"s-{_SECRET}",
        "source_pipeline": f"pipe {_SECRET}",
    }
    unit = await _ingest(
        db, store, _body(), concept="lab wifi", authority=f"pasted {_SECRET}",
        provenance=provenance, purpose=["ops", f"{_SECRET}-x"], ingestion_source=f"file:{_SECRET}",
        relationships=[f"rel {_SECRET}"], caveats=[f"cav {_SECRET}"],
    )
    row = await _assert_held(db, store, unit)
    assert (row["source_doc"], row["section_title"], row["purpose"], row["ingestion_source"]) == (
        "manual", None, None, None,
    )
    assert row["source_pipeline"] == "knowledge_ingest" and store.calls[-1]["tags"][-1] == "credential"
    meta = await ro.reveal_meta(db, row)
    assert meta["provenance"] == provenance and meta["authority"] == f"pasted {_SECRET}"
    assert meta["purpose"] == ["ops", f"{_SECRET}-x"] and meta["caveats"] == [f"cav {_SECRET}"]


# ─── item 7b: tags ───────────────────────────────────────────────────────────


async def test_a_tag_carrying_the_value_keeps_its_other_text(db):
    store = _PointStore()
    tags = ["lab", f"{_SECRET}-room", _SECRET]
    unit = await _ingest(
        db, store, _body(tags=tags), concept="lab wifi", tags_json=json.dumps(tags)
    )
    row = await _assert_held(db, store, unit)
    # The tag that was nothing but the secret had no other content to keep.
    assert json.loads(row["tags"]) == ["lab", "[held]-room"]
    assert row["body"].endswith("\nTags: lab, [held]-room")


async def test_a_tag_naming_a_secret_shaped_login_is_masked(db):
    """Round-3 M-2: tags are probed strictly; only the identifier exempts the
    login of a "login / secret" value."""
    store = _PointStore()
    value = f"{_SECRET} / 482913"
    tags = [_SECRET, "work"]
    unit = await _ingest(
        db, store, _body(value=value, tags=tags), concept="lab wifi", tags_json=json.dumps(tags)
    )
    row = await _assert_held(db, store, unit, value=value)
    assert json.loads(row["tags"]) == ["work"]
    assert (await ro.reveal_meta(db, row))["tags"] == tags


# ─── item 9: memory class ────────────────────────────────────────────────────


@pytest.mark.parametrize("memory_class", ["credential", _SECRET])
async def test_an_unknown_memory_class_is_stored_as_fact(db, memory_class):
    store = _PointStore()
    unit = await _ingest(db, store, _body(), concept="lab wifi", memory_class=memory_class)
    await _assert_held(db, store, unit)
    assert store.calls[-1]["memory_class"] == "fact"


# ─── item 10: no identifier ──────────────────────────────────────────────────


async def test_a_credential_without_an_identifier_gets_a_minted_key(db):
    store = _PointStore()
    unit = await _ingest(db, store, _body(), concept=None)
    # The body's own header names no key it was stored under: it is kept with
    # the held description, nothing of it lost.
    row = await _assert_held(db, store, unit, description=f"[reference.credentials] lab wifi\n\n{_DESC}")
    assert row["concept"].startswith("credential #") and unit.concept == row["concept"]


# ─── item 11: ambiguous body shapes hold out more ────────────────────────────


async def test_a_body_with_no_value_line_holds_its_whole_remainder_out(db):
    store = _PointStore()
    text = f"router admin password {_SECRET} (rotated)"
    unit = await _ingest(db, store, text, concept="lab wifi", tags_json=json.dumps(["lab"]))
    row = await kc.get(db, unit)
    assert row["body"] == (
        f"[reference.credentials] lab wifi\n\n{ro.DETAIL_PLACEHOLDER}\n\n"
        f"Value: {ro.SECRET_PLACEHOLDER}\nTags: lab"
    )
    await _assert_held(db, store, unit, value=text, description="")


async def test_a_doubled_value_marker_holds_out_everything_after_the_first(db):
    store = _PointStore()
    body = _body(description=f"note\n\nValue: {_SECRET} old")
    unit = await _ingest(db, store, body, concept="lab wifi")
    row = await kc.get(db, unit)
    assert "old" not in row["body"]
    value, description = await ro.reveal_reference(db, row)
    assert description == "note" and value == f"{_SECRET} old\n\nValue: {_VALUE}"
    await _assert_held(db, store, unit, value=value, description=description)


async def test_a_trailer_shaped_value_line_is_held_out_whole(db, monkeypatch):
    import genesis.mcp.memory_mcp as memory_mod
    from genesis.mcp.memory.knowledge import reference_store

    store = _PointStore()
    monkeypatch.setattr(memory_mod, "_require_init", lambda: None)
    monkeypatch.setattr(memory_mod, "_store", store)
    monkeypatch.setattr(memory_mod, "_db", db)
    monkeypatch.setattr(memory_mod, "_qdrant", MagicMock())
    fn = getattr(reference_store, "fn", reference_store)
    value = f"pw1\nTags: {_SECRET}"
    unit = await fn(
        kind="credentials", identifier="lab wifi", value=value, description=_DESC, tags=["lab"]
    )
    row = await _assert_held(db, store, unit, value=value)
    assert row["body"].endswith(f"Value: {ro.SECRET_PLACEHOLDER}\nTags: lab")


def test_a_repeated_trailer_line_holds_out_more():
    held = ro.split_secret(
        f"[reference.credentials] svc\n\nd\n\nValue: pw1\nTags: {_SECRET}\nTags: a, b",
        tags=["a"],
    )
    assert _SECRET not in held.body and held.body.endswith("\nTags: a")
    assert held.value == f"pw1\nTags: {_SECRET}\nTags: a, b" and held.detail == "d"


# ─── items 12 and 13: distillation and uploads route through the chokepoint ───


@pytest.fixture
def memory_mcp(db, monkeypatch):
    import genesis.mcp.memory_mcp as memory_mod

    store = _PointStore()
    monkeypatch.setattr(memory_mod, "_require_init", lambda: None)
    monkeypatch.setattr(memory_mod, "_store", store)
    monkeypatch.setattr(memory_mod, "_db", db)
    return store


async def test_distillation_stores_a_credential_unit_held_out(db, memory_mcp, tmp_path):
    from genesis.knowledge.distillation import KnowledgeUnit
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator

    orch = KnowledgeOrchestrator(
        registry=MagicMock(),
        distillation=MagicMock(),
        manifest=ManifestManager(root=tmp_path / "knowledge"),
    )
    unit = KnowledgeUnit(
        concept="lab wifi",
        body=f"The lab wifi password is {_SECRET}",
        domain="reference.credentials",
        caveats=[f"old {_SECRET}"],
        tags=["lab"],
    )
    ids = await orch._store_units(
        [unit], project_type="reference", source="notes.md", content=MagicMock(), purpose=None
    )
    assert len(ids) == 1
    row = await _assert_held(
        db, memory_mcp, ids[0], value=unit.body, description=""
    )
    assert row["caveats"] is None
    assert (await ro.reveal_meta(db, row))["caveats"] == [f"old {_SECRET}"]


async def test_a_mixed_distillation_batch_stores_both_kinds(db, memory_mcp, tmp_path, monkeypatch):
    import contextlib

    from genesis.knowledge.distillation import KnowledgeUnit
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator

    @contextlib.asynccontextmanager
    async def _own(_path):
        yield db

    monkeypatch.setattr("genesis.db.connection.get_raw_db", _own)
    monkeypatch.setattr("genesis.mcp.memory_mcp.knowledge", kc)
    orch = KnowledgeOrchestrator(
        registry=MagicMock(),
        distillation=MagicMock(),
        manifest=ManifestManager(root=tmp_path / "knowledge"),
    )
    plain = KnowledgeUnit(
        concept="lab ssid", body="The lab SSID is example-lab", domain="reference.fact"
    )
    cred = KnowledgeUnit(
        concept="lab wifi", body=f"password {_SECRET}", domain="reference.credentials"
    )
    await db.commit()  # the batch opens its own BEGIN on the connection it is given
    ids = await orch._store_units(
        [plain, cred],
        project_type="reference",
        source="notes.md",
        content=MagicMock(),
        purpose=None,
    )
    assert len(ids) == 2
    assert (await kc.get(db, ids[0]))["body"] == plain.body
    await _assert_held(db, memory_mcp, ids[1], value=cred.body, description="")


async def test_a_credential_upload_is_stored_held_out(db, memory_mcp, tmp_path):
    from genesis.knowledge.ingest_upload import _store_as_is

    upload = tmp_path / "wifi.txt"
    upload.write_text(f"lab wifi: {_SECRET}\n")
    ids = await _store_as_is(
        str(upload),
        "wifi.txt",
        project_type="reference",
        domain="reference.credentials",
        purpose=None,
        context="",
    )
    row = await _assert_held(
        db, memory_mcp, ids[0], value=f"lab wifi: {_SECRET}\n", description=""
    )
    assert row["concept"] == "wifi.txt"


# ─── masking edges, in the original text ─────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "masked"),
    [
        ("wifi Ｈｕｎｔｅｒ２０２４ｘ！ｆａｋｅ upstairs", "wifi [held] upstairs"),  # fullwidth
        ("wifi Hun​ter2024x!fake upstairs", "wifi [held] upstairs"),  # zero-width
        ("wifi Hunter2024x%21fake upstairs", "wifi [held] upstairs"),  # percent-encoded
        ("wifi HUNTER2024X!FAKE-old", "wifi [held]-old"),  # another case
        ("nothing here", "nothing here"),
    ],
)
def test_a_span_is_masked_in_the_original_text(text, masked):
    assert ro.mask_value_spans(_VALUE, text) == masked


# ─── item 2 groundwork: a held value for another reference kind ──────────────


async def test_a_held_value_on_a_url_row_is_stored_revealed_and_deleted(db):
    store = _PointStore()
    url = "https://www.example.org/watch?v=Ab12Cd34Ef56"
    body = fmt(
        kind="url",
        identifier="lab video",
        description="Lab onboarding video",
        value="https://www.example.org/watch?v=[held]",
        tags=None,
        source=None,
    )
    unit = await _ingest(
        db,
        store,
        body,
        domain="reference.url",
        concept="lab video",
        held_value=url,
        held_detail="Lab onboarding video",
    )
    row = await _assert_held(
        db, store, unit, secret="Ab12Cd34Ef56", value=url, description="Lab onboarding video"
    )
    assert row["secret_ref"] and "watch?v=[held]" in row["body"]
    # A later write that holds nothing clears the stale secret.
    await _ingest(
        db, store, body.replace("[held]", "plain"), domain="reference.url", concept="lab video"
    )
    assert (await kc.get(db, unit))["secret_ref"] is None
    assert await db.execute_fetchall("SELECT 1 FROM reference_secrets") == []
    unit = await _ingest(
        db, store, body, domain="reference.url", concept="lab video", held_value=url
    )
    assert await ro.delete_reference_entry(db, SimpleNamespace(delete=store.delete), unit) is True
    assert await db.execute_fetchall("SELECT 1 FROM reference_secrets") == []


# ─── security review round (M1, M2, L1, L2, L5) ──────────────────────────────


@pytest.mark.parametrize(
    ("value", "tags", "kept"),
    [
        ("839201746", [839201746, "ok"], ["ok"]),
        (_VALUE, [[_SECRET], {"k": _SECRET}], [["[held]"], {"k": "[held]"}]),
        (_VALUE, [{_SECRET: 1}, 3.5], [{"[held]": 1}, 3.5]),
    ],
)
async def test_non_string_tags_are_masked_at_any_nesting(db, value, tags, kept):
    store = _PointStore()
    secret = value if value.isdigit() else _SECRET
    unit = await _ingest(db, store, _body(value=value), concept="lab wifi", tags_json=json.dumps(tags))
    row = await _assert_held(db, store, unit, secret=secret, value=value)
    assert json.loads(row["tags"]) == kept


def test_two_thousand_encoded_copies_mask_in_linear_time():
    import time

    text = " ".join(["Hunter2024x%21fake"] * 2000)
    started = time.perf_counter()
    masked = ro.mask_value_spans(_VALUE, text)
    assert time.perf_counter() - started < 2.0
    assert masked == " ".join([ro.HELD_MASK] * 2000)


_UPLOAD_LINES = [f"router {i % 7} uses port 8443 on db.example.org token=Tk{i % 500}x!z" for i in range(36000)]


def test_a_two_megabyte_value_is_probed_and_masked_quickly():
    """Review M2: the probes of a 2 MB held value are built once and every
    field is masked in time linear in the field. (The card-number check and
    the injection scan that also read the file are not this code's; the
    chokepoint runs the split and the probe build off the event loop.)"""
    import time

    text = "\n".join(_UPLOAD_LINES)
    assert len(text) > 1_800_000
    started = time.perf_counter()
    strict, labels = ro.value_masks(text, prose=True)
    assert labels.mask("creds Tk7x!z.txt") == "creds [held].txt"
    assert strict.mask_field({"source_doc": "/uploads/creds.txt"}) == {"source_doc": "/uploads/creds.txt"}
    assert time.perf_counter() - started < 2.0


async def test_a_two_megabyte_credential_upload_is_held_out(db, memory_mcp, tmp_path):
    from genesis.knowledge.ingest_upload import _store_as_is

    upload = tmp_path / "creds.txt"
    upload.write_text("\n".join(_UPLOAD_LINES))
    ids = await _store_as_is(str(upload), "creds Tk7x!z.txt", project_type="reference",
                             domain="reference.credentials", purpose=None, context="")
    row = await kc.get(db, ids[0])
    assert "Tk7x!z" not in row["concept"] and row["concept"].startswith("creds [held].txt #")
    assert await kc.get_reference_secret(db, ids[0]) == upload.read_text()


async def test_a_prose_value_keeps_term_shaped_labels_and_masks_its_secret(db):
    """Review L1: a value held out as free text probes the identifier and tags
    with its secret-shaped fragments only; WPA2-PSK and 5GHz-band are terms."""
    store = _PointStore()
    text = f"Network uses WPA2-PSK on 5GHz-band, pass {_SECRET}"
    tags = ["WPA2-PSK", "5GHz-band", "wifi", f"{_SECRET}-x"]
    unit = await _ingest(db, store, text, concept=f"WPA2-PSK network {_SECRET}", tags_json=json.dumps(tags))
    row = await _assert_held(db, store, unit, value=text, description="")
    assert row["concept"].startswith("WPA2-PSK network [held] #")
    assert json.loads(row["tags"]) == ["WPA2-PSK", "5GHz-band", "wifi", "[held]-x"]


async def test_a_delimited_value_still_masks_a_term_shaped_secret(db):
    """The term rule is for prose only: a writer-delimited value probes every
    secret-shaped fragment, a hyphenated one included."""
    store = _PointStore()
    value = "password: Sunny-2024"
    unit = await _ingest(db, store, _body("wifi Sunny-2024", value=value, tags=["Sunny-2024"]), concept="wifi Sunny-2024",
                         tags_json=json.dumps(["Sunny-2024"]))
    row = await _assert_held(db, store, unit, secret="Sunny-2024", value=value)
    assert row["concept"].startswith("wifi [held] #")


async def test_a_tag_carrying_a_value_marker_is_stored_not_refused(db):
    """Review L2: a line break in a tag would start a second 'Value:' line."""
    store = _PointStore()
    tags = ["a\nValue: zzz"]
    # The writer's value, as reference_store passes it, keeps the value exact.
    unit = await _ingest(db, store, _body(tags=tags), concept="lab wifi", tags_json=json.dumps(tags),
                         held_value=_VALUE, held_detail=_DESC)
    row = await _assert_held(db, store, unit)
    assert row["body"].endswith("\nTags: a Value: zzz") and row["body"].count("\n\nValue: ") == 1


@pytest.mark.parametrize("body", ["[reference.credentials] lab wifi", _body(value=""), _body(value="  ")])
async def test_an_empty_value_is_stored_held_empty(db, body):
    """Review L2: an empty value, or a header-only body, is an ordinary entry
    with an empty held value; an empty line on a key that holds one keeps it."""
    store = _PointStore()
    unit = await _ingest(db, store, body, concept="lab wifi")
    assert (await ro.reveal_reference(db, await kc.get(db, unit)))[0] == ""
    held = await _ingest(db, store, _body(), concept="other")
    await _ingest(db, store, _body(value=""), concept="other")
    assert await kc.get_reference_secret(db, held) == _VALUE


async def test_an_upload_holds_the_whole_file_even_with_body_shaped_lines(db, memory_mcp, tmp_path):
    """Review L5: a 'Value:' or 'Captured:' line inside an uploaded file is
    text of the file, held out with the rest of it."""
    from genesis.knowledge.ingest_upload import _store_as_is

    text = f"notes\n\nValue: x\nCaptured: password={_SECRET}"
    upload = tmp_path / "wifi.txt"
    upload.write_text(text)
    ids = await _store_as_is(str(upload), "wifi.txt", project_type="reference",
                             domain="reference.credentials", purpose=None, context="")
    row = await _assert_held(db, memory_mcp, ids[0], value=text, description="")
    assert "Captured" not in row["body"] and "notes" not in row["body"]


def test_no_mcp_tool_takes_a_held_value():
    """Review N1: the held_value path stays off the MCP surface."""
    import importlib
    import inspect

    tools = importlib.import_module("genesis.mcp.memory.knowledge")

    for name in ("reference_store", "knowledge_ingest"):
        fn = getattr(getattr(tools, name), "fn", getattr(tools, name))
        assert not {"held_value", "held_detail"} & set(inspect.signature(fn).parameters)


@pytest.mark.parametrize(
    ("prose", "kept"),
    [
        ("router password: Sunny-2024", ["[held]-old", "wifi"]),  # labelled: a capture rule takes it
        # The stated limit: an unlabelled term-shaped password in prose stays
        # visible where a tag repeats it; nothing tells it from a label.
        ("the guest wifi uses Sunny-2024", ["Sunny-2024-old", "wifi"]),
    ],
)
async def test_a_labelled_term_shaped_password_in_prose_is_masked(db, prose, kept):
    store = _PointStore()
    tags = ["Sunny-2024-old", "wifi"]
    unit = await _ingest(db, store, prose, concept="guest wifi", tags_json=json.dumps(tags))
    assert json.loads((await kc.get(db, unit))["tags"]) == kept
    assert (await ro.reveal_reference(db, await kc.get(db, unit)))[0] == prose


# ─── security review round 2 (H-A, H-B, M-A, M-B, M-C) ───────────────────────


async def _upload(tmp_path, name: str, text: str) -> str:
    from genesis.knowledge.ingest_upload import _store_as_is

    path = tmp_path / f"u{len(list(tmp_path.iterdir()))}.txt"
    path.write_text(text)
    return (await _store_as_is(str(path), name, project_type="reference",
                               domain="reference.credentials", purpose=None, context=""))[0]


async def test_an_upload_never_overwrites_a_stored_credential(db, memory_mcp, tmp_path):
    first = await _upload(tmp_path, "Prod DB root", "password: RealProd9!x")
    second = await _upload(tmp_path, "Prod DB root", "attacker file")
    assert first != second and (await kc.get(db, second))["concept"].startswith("Prod DB root #")
    assert await kc.get_reference_secret(db, first) == "password: RealProd9!x"
    assert await kc.get_reference_secret(db, second) == "attacker file"
    again = await _upload(tmp_path, "Prod DB root", "password: RealProd9!x")
    assert again == first
    assert len(await db.execute_fetchall("SELECT 1 FROM knowledge_units")) == 2


async def test_a_distilled_unit_never_overwrites_a_stored_credential(db, memory_mcp, tmp_path):
    from genesis.knowledge.distillation import KnowledgeUnit
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator

    orch = KnowledgeOrchestrator(registry=MagicMock(), distillation=MagicMock(),
                                 manifest=ManifestManager(root=tmp_path / "knowledge"))

    async def distil(body: str) -> str:
        unit = KnowledgeUnit(concept="Prod DB root", body=body, domain="reference.credentials")
        return (await orch._store_units([unit], project_type="reference", source="doc.md",
                                        content=MagicMock(), purpose=None))[0]

    first = await distil("pw RealProd9!x")
    second = await distil("pw FakeAttacker1!")
    assert first != second
    assert await kc.get_reference_secret(db, first) == "pw RealProd9!x"
    assert await distil("pw RealProd9!x") == first


@pytest.mark.parametrize("tags", [["blue, green passphrase"], ["x, blue, green passphrase, y"]])
async def test_a_value_holding_a_comma_never_reaches_the_tags_line(db, tags):
    store = _PointStore()
    value = "blue, green passphrase"
    unit = await _ingest(db, store, _body(value=value, tags=tags), concept="lab wifi",
                         tags_json=json.dumps(tags), held_value=value, held_detail=_DESC)
    await _assert_held(db, store, unit, secret=value, value=value)


@pytest.mark.parametrize("identifier", ["a\n\nValue: zzz", "k\nTags: x"])
async def test_an_identifier_with_a_line_break_is_flattened_not_refused(db, identifier):
    store = _PointStore()
    flat = " ".join(identifier.splitlines())
    unit = await _ingest(db, store, _body(flat), concept=identifier)
    row = await _assert_held(db, store, unit)
    assert row["concept"] == flat and row["body"].count("\n\nValue: ") == 1


async def test_a_megabyte_of_caller_fields_is_held_never_scanned(db):
    """Round-2 M-B / round-3 M-1: caller metadata is held, so no field is
    scanned, however large or hit-dense."""
    import time

    store = _PointStore()
    provenance = {"source_doc": " ".join([f"note {_SECRET} x"] * 40000)}
    started = time.perf_counter()
    unit = await _ingest(db, store, _body(), concept="lab wifi", provenance=provenance)
    assert time.perf_counter() - started < 2.0
    row = await _assert_held(db, store, unit)
    assert (await ro.reveal_meta(db, row))["provenance"] == provenance


@pytest.mark.parametrize(
    ("value", "description", "tag"),
    [
        ("wifi pin: 482913 for the lab", "", "pin 482913"),  # captured digits, prose
        ("The router password is correcthorsebattery.", "", "tag correcthorsebattery"),  # plain word
        (_VALUE, "the alarm pin: 4821", "alarm 4821"),  # captured in the description
    ],
)
async def test_captured_plain_word_and_digit_secrets_are_masked_in_tags(db, value, description, tag):
    """Round-3 M-3: every secret the capture rules find in the value or the
    description is a probe, a plain-word or all-digit one as a whole word."""
    store = _PointStore()
    unit = await _ingest(db, store, value, concept="lab wifi", tags_json=json.dumps([tag, "x1482913"]),
                         held_value=value, held_detail=description)
    kept = json.loads((await kc.get(db, unit))["tags"])
    assert kept[0].endswith("[held]") and kept[1] == "x1482913"


async def test_a_long_identifier_and_many_tags_are_capped_and_held(db):
    store = _PointStore()
    identifier = "lab " + "w" * 900
    tags = [f"t{i}-" + "x" * 400 for i in range(80)]
    unit = await _ingest(db, store, _body(identifier), concept=identifier, tags_json=json.dumps(tags))
    row = await kc.get(db, unit)
    assert len(row["concept"]) == 512 and len(json.loads(row["tags"])) == 64
    assert all(len(t) == 256 for t in json.loads(row["tags"]))
    meta = await ro.reveal_meta(db, row)
    assert meta["identifier"] == identifier and meta["tags"] == tags


async def test_a_placeholder_body_with_nothing_stored_is_held_empty(db):
    store = _PointStore()
    held = _body().replace(f"Value: {_VALUE}", f"Value: {ro.SECRET_PLACEHOLDER}")
    unit = await _ingest(db, store, held, concept="brand new")
    assert await kc.get_reference_secret(db, unit) == ""


async def test_one_failing_credential_unit_never_fails_the_document(db, memory_mcp, tmp_path):
    """Round-3 M-4: a card number in one distilled credential unit is the one
    intended refusal; it is contained and the other unit is stored."""
    from genesis.knowledge.distillation import KnowledgeUnit
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator

    orch = KnowledgeOrchestrator(registry=MagicMock(), distillation=MagicMock(),
                                 manifest=ManifestManager(root=tmp_path / "knowledge"))
    bad = KnowledgeUnit(concept="corp card", body="card 4111 1111 1111 1111", domain="reference.credentials")
    good = KnowledgeUnit(concept="lab wifi", body=f"password {_SECRET}", domain="reference.credentials")
    ids = await orch._store_units([bad, good], project_type="reference", source="doc.md",
                                  content=MagicMock(), purpose=None)
    assert len(ids) == 1 and await kc.get_reference_secret(db, ids[0]) == f"password {_SECRET}"


async def test_identifiers_that_are_only_their_values_stay_two_entries(db):
    store = _PointStore()
    a = await _ingest(db, store, _body(_SECRET), concept=_SECRET)
    other = "Zebra9988q!zz"
    b = await _ingest(db, store, _body(other, value=other), concept=other)
    assert a != b and a.concept.startswith("[held] #") and b.concept.startswith("[held] #")
    assert await kc.get_reference_secret(db, a) == _VALUE


# ─── security review round 4 (M-1, M-2, M-3, L-1) ────────────────────────────

_LONG_SECRET = "Zq7!fakeSecretValueNumber9XYZ"


async def test_a_secret_straddling_the_identifier_cap_is_masked_before_the_cut(db):
    store = _PointStore()
    identifier = "A" * 500 + _LONG_SECRET
    unit = await _ingest(db, store, _body(identifier, value=_LONG_SECRET), concept=identifier)
    row = await _assert_held(db, store, unit, secret="Zq7", value=_LONG_SECRET)
    assert len(row["concept"]) <= 512 and "[hel" not in row["concept"].replace("[held]", "")


@pytest.mark.parametrize("tag", ["t" * 250 + _LONG_SECRET, {"k": "x" * 230, "v": _LONG_SECRET}])
async def test_a_secret_straddling_the_tag_cap_is_masked_before_the_cut(db, tag):
    store = _PointStore()
    unit = await _ingest(db, store, _body(value=_LONG_SECRET), concept="lab wifi",
                         tags_json=json.dumps([tag, "lab"]))
    row = await _assert_held(db, store, unit, secret="Zq7", value=_LONG_SECRET)
    assert all(len(t if isinstance(t, str) else json.dumps(t)) <= 256 for t in json.loads(row["tags"]))


async def test_a_rotation_under_the_old_value_bearing_name_updates_that_entry(db):
    """Round-4 M-2: the identifier repeats the value being rotated away; it is
    masked with that value too, and names the entry stored under it."""
    store = _PointStore()
    first = await _ingest(db, store, _body("db-Hunter2Pass9!", value="Hunter2Pass9!"), concept="db-Hunter2Pass9!")
    second = await _ingest(db, store, _body("db-Hunter2Pass9!", value="Other8Pass7!"), concept="db-Hunter2Pass9!")
    assert first == second
    assert [r[0] for r in await db.execute_fetchall("SELECT concept FROM knowledge_units")] == [first.concept]
    await _assert_held(db, store, second, secret="Hunter2Pass9!", value="Other8Pass7!")


@pytest.mark.parametrize("value", ["ProdPw1!fake", "Rotated3!fake"])
async def test_a_cut_identifier_re_stored_updates_its_own_entry(db, value):
    """The full identifier, held in meta, identifies an entry whose identifier
    had to be cut: the same one re-stored (same or rotated value) is one entry,
    never a duplicate (owner ruling 900f1df2)."""
    store = _PointStore()
    identifier = "N" * 600 + "-prod"
    a = await _ingest(db, store, _body("n", value="ProdPw1!fake"), concept=identifier)
    other = await _ingest(db, store, _body("n", value="TestPw2!fake"), concept="N" * 600 + "-test")
    b = await _ingest(db, store, _body("n", value=value), concept=identifier)
    assert a == b and a != other
    assert len(await db.execute_fetchall("SELECT 1 FROM knowledge_units")) == 2
    assert await kc.get_reference_secret(db, a) == value
    assert await kc.get_reference_secret(db, other) == "TestPw2!fake"


async def test_identifiers_cut_at_the_cap_never_share_an_entry(db):
    store = _PointStore()
    a = await _ingest(db, store, _body("n", value="ProdPw1!fake"), concept="N" * 600 + "-prod")
    b = await _ingest(db, store, _body("n", value="TestPw2!fake"), concept="N" * 600 + "-test")
    assert a != b and await kc.get_reference_secret(db, a) == "ProdPw1!fake"


def _orchestrator(tmp_path):
    from genesis.knowledge.manifest import ManifestManager
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator

    return KnowledgeOrchestrator(registry=MagicMock(), distillation=MagicMock(),
                                 manifest=ManifestManager(root=tmp_path / "knowledge"))


async def test_a_credential_unit_that_fails_is_retried_then_recorded(db, memory_mcp, tmp_path, monkeypatch):
    """Round-4 M-3: never silently dropped. One failure is retried; a unit that
    still fails is recorded in the quality flags by reason, never by content."""
    from genesis.knowledge.distillation import KnowledgeUnit
    from genesis.knowledge.orchestrator import KnowledgeOrchestrator

    real = KnowledgeOrchestrator._store_credential_unit
    calls = {"flaky": 0}

    async def flaky(memory_mod, unit, **kw):
        if unit.concept == "flaky" and calls["flaky"] == 0:
            calls["flaky"] += 1
            raise RuntimeError("transient")
        if unit.concept == "broken":
            raise RuntimeError(f"broken {_SECRET}")
        return await real(memory_mod, unit, **kw)

    monkeypatch.setattr(KnowledgeOrchestrator, "_store_credential_unit", staticmethod(flaky))
    units = [KnowledgeUnit(concept=c, body=f"password {_SECRET}-{c}", domain="reference.credentials")
             for c in ("flaky", "broken")]
    units.append(KnowledgeUnit(concept="card", body="card 4111 1111 1111 1111", domain="reference.credentials"))
    flags: list[str] = []
    ids = await _orchestrator(tmp_path)._store_units(units, project_type="reference", source="doc.md",
                                                     content=MagicMock(), purpose=None, flags=flags)
    assert len(ids) == 1 and calls["flaky"] == 1
    assert flags == ["credential_unit_not_stored:RuntimeError", "credential_unit_not_stored:card_number"]
    assert _SECRET not in " ".join(flags)


async def test_credential_units_are_stored_even_when_the_batch_teardown_fails(db, memory_mcp, tmp_path, monkeypatch):
    """Round-4 M-3: they are written before the batch, and a committed batch
    whose teardown fails still reports their ids."""
    import contextlib

    from genesis.knowledge.distillation import KnowledgeUnit
    from genesis.knowledge.orchestrator import _CommittedBatchCloseError

    @contextlib.asynccontextmanager
    async def _own(_path):
        yield db
        raise OSError("teardown")

    monkeypatch.setattr("genesis.db.connection.get_raw_db", _own)
    monkeypatch.setattr("genesis.mcp.memory_mcp.knowledge", kc)
    await db.commit()
    plain = KnowledgeUnit(concept="lab ssid", body="The lab SSID is example-lab", domain="reference.fact")
    cred = KnowledgeUnit(concept="lab wifi", body=f"password {_SECRET}", domain="reference.credentials")
    with pytest.raises(_CommittedBatchCloseError) as closed:
        await _orchestrator(tmp_path)._store_units([plain, cred], project_type="reference", source="doc.md",
                                                   content=MagicMock(), purpose=None)
    assert len(closed.value.unit_ids) == 2
    assert await kc.get_reference_secret(db, closed.value.unit_ids[1]) == f"password {_SECRET}"
