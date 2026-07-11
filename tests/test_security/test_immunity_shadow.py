"""WS-3 B1 emit layer — security.immunity_shadow.

The emit is the ONE place the never-block invariant + kill switch are enforced
for shadow recording: it writes a would-block row ONLY when the gate is not
off, the origin is blockable (external_untrusted), and at least one item was
blockable. owner/first_party never produce a row; master ``enabled=false``
short-circuits live.
"""

from __future__ import annotations

import contextlib
import sqlite3

import aiosqlite
import pytest

from genesis.db.crud import immunity_shadow as crud
from genesis.db.schema._tables import INDEXES, TABLES
from genesis.security import immunity, immunity_shadow


@pytest.fixture(autouse=True)
def _reset_caches():
    crud._table_verified = False
    crud._table_verified_sync = False
    yield
    crud._table_verified = False
    crud._table_verified_sync = False


async def _db(path):
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    await conn.execute(TABLES["immunity_shadow_events"])
    for idx in INDEXES:
        if "immunity_shadow_events" in idx:
            await conn.execute(idx)
    await conn.commit()
    return conn


def _sync_db(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(TABLES["immunity_shadow_events"])
    conn.commit()
    return conn


_SITE = dict(
    gate="injection",
    source_kind="recall_inject",
    source_ref="mcp/memory/core.py::memory_recall",
    process="server",
)


@pytest.mark.asyncio
async def test_shadow_external_records_one_row(tmp_path, monkeypatch):
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "shadow")
    db = await _db(tmp_path / "g.db")
    wrote = await immunity_shadow.record_would_block(
        **_SITE,
        blockable_count=3,
        origin_class="external_untrusted",
        db=db,
    )
    assert wrote is True
    rows = await crud.list_recent(db)
    assert len(rows) == 1
    r = rows[0]
    assert r["gate"] == "injection" and r["mode"] == "shadow"
    assert r["origin_class"] == "external_untrusted" and r["would_block"] == 1
    assert '"blockable":3' in r["detail"]
    await db.close()


@pytest.mark.asyncio
async def test_zero_blockable_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "shadow")
    db = await _db(tmp_path / "g.db")
    wrote = await immunity_shadow.record_would_block(
        **_SITE,
        blockable_count=0,
        origin_class="external_untrusted",
        db=db,
    )
    assert wrote is False
    assert await crud.count(db) == 0
    await db.close()


@pytest.mark.asyncio
async def test_gate_off_short_circuits(tmp_path, monkeypatch):
    # Kill switch: master enabled=false → gate_mode returns "off" → no row.
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "off")
    db = await _db(tmp_path / "g.db")
    wrote = await immunity_shadow.record_would_block(
        **_SITE,
        blockable_count=5,
        origin_class="external_untrusted",
        db=db,
    )
    assert wrote is False
    assert await crud.count(db) == 0
    await db.close()


@pytest.mark.asyncio
async def test_first_party_never_recorded(tmp_path, monkeypatch):
    # The never-block invariant — enforced centrally in the emit via is_blockable.
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "shadow")
    db = await _db(tmp_path / "g.db")
    for oc in ("first_party", "owner"):
        wrote = await immunity_shadow.record_would_block(
            **_SITE,
            blockable_count=9,
            origin_class=oc,
            db=db,
        )
        assert wrote is False
    assert await crud.count(db) == 0
    await db.close()


@pytest.mark.asyncio
async def test_auto_demote_noop_in_shadow(tmp_path, monkeypatch):
    # Even over the would-block threshold, shadow mode must NEVER demote.
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "shadow")
    demoted = []
    monkeypatch.setattr(immunity, "record_demotion", lambda g, r: demoted.append((g, r)))
    db = await _db(tmp_path / "g.db")
    for _ in range(20):
        await immunity_shadow.record_would_block(
            **_SITE,
            blockable_count=1,
            origin_class="external_untrusted",
            db=db,
        )
    assert demoted == []  # dormant until B4 flips a gate to enforce
    await db.close()


@pytest.mark.asyncio
async def test_db_none_self_resolves(tmp_path, monkeypatch):
    # Retriever-based sites (research/voice/context_injector) pass db=None; the
    # emit opens its own short-lived connection.
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "shadow")
    db = await _db(tmp_path / "resolved.db")

    @contextlib.asynccontextmanager
    async def _fake_raw():
        yield db

    monkeypatch.setattr(immunity_shadow, "get_raw_db", _fake_raw)
    wrote = await immunity_shadow.record_would_block(
        gate="injection",
        source_kind="recall_inject",
        source_ref="autonomy/executor/research.py::_memory_recall",
        process="server",
        blockable_count=2,
        origin_class="external_untrusted",
        db=None,
    )
    assert wrote is True
    assert await crud.count(db) == 1
    await db.close()


def test_sync_emit_records(tmp_path, monkeypatch):
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "shadow")
    conn = _sync_db(tmp_path / "g.db")
    wrote = immunity_shadow.record_would_block_sync(
        conn,
        gate="injection",
        source_kind="proactive_hook",
        source_ref="scripts/proactive_memory_hook.py::_format_results",
        blockable_count=1,
        origin_class="external_untrusted",
    )
    assert wrote is True
    assert conn.execute("SELECT COUNT(*) FROM immunity_shadow_events").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize(
    "collection,source_pipeline,expected",
    [
        ("knowledge_base", "knowledge_ingest", True),  # external KB content
        ("knowledge_base", None, True),  # KB default → external
        ("knowledge_base", "surplus", False),  # first-party pipeline IN kb
        ("knowledge_base", "reference_store", False),  # first-party pipeline IN kb
        ("episodic_memory", "conversation", False),  # not external at all
        (None, None, False),  # unknown → not external
    ],
)
def test_item_is_blockable_honors_first_party_in_kb(collection, source_pipeline, expected):
    assert (
        immunity_shadow.item_is_blockable(collection=collection, source_pipeline=source_pipeline)
        is expected
    )


@pytest.mark.asyncio
async def test_recent_summary_rolls_up(tmp_path, monkeypatch):
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "shadow")
    db = await _db(tmp_path / "g.db")
    for _ in range(3):
        await immunity_shadow.record_would_block(
            **_SITE,
            blockable_count=1,
            origin_class="external_untrusted",
            db=db,
        )
    rows = await immunity_shadow.recent_summary(db=db)
    assert rows and rows[0]["n"] == 3
    assert rows[0]["source_ref"] == _SITE["source_ref"]
    await db.close()


def test_sync_emit_gate_off(tmp_path, monkeypatch):
    monkeypatch.setattr(immunity, "gate_mode", lambda g: "off")
    conn = _sync_db(tmp_path / "g.db")
    wrote = immunity_shadow.record_would_block_sync(
        conn,
        gate="injection",
        source_kind="proactive_hook",
        source_ref="scripts/proactive_memory_hook.py::_format_results",
        blockable_count=1,
        origin_class="external_untrusted",
    )
    assert wrote is False
    conn.close()
