"""WS-3 B1 gates 2-3 — emit contract under the REAL config + migration chain.

Mirrors test_procedure_gate.py's integration shape: external origin → one row,
owner/first_party → never a row (never-block invariant), kill switch → nothing,
for both the identity and autonomy gates. Also drives the REAL crud choke
(capability_grants mutators) end-to-end so the emit placement is proven, not
assumed.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import capability_grants as cg
from genesis.db.crud import immunity_shadow as crud
from genesis.db.migrations.runner import MigrationRunner
from genesis.security import immunity, immunity_shadow


@pytest.fixture(autouse=True)
def _reset_caches():
    crud._table_verified = False
    crud._table_verified_sync = False
    yield
    crud._table_verified = False
    crud._table_verified_sync = False


async def _migrated(path):
    db = await aiosqlite.connect(str(path))
    db.row_factory = aiosqlite.Row
    await MigrationRunner(db).run_pending()
    return db


@pytest.mark.asyncio
@pytest.mark.parametrize("gate", ["identity", "autonomy"])
async def test_gate_records_external_never_owner_or_firstparty(tmp_path, gate):
    mode = immunity.gate_mode(gate)
    assert mode in ("off", "shadow", "enforce")
    db = await _migrated(tmp_path / "g.db")

    wrote_ext = await immunity_shadow.record_would_block(
        gate=gate,
        source_kind="test",
        source_ref="t",
        process="server",
        blockable_count=1,
        origin_class="external_untrusted",
        db=db,
    )
    wrote_fp = await immunity_shadow.record_would_block(
        gate=gate,
        source_kind="test",
        source_ref="t",
        process="server",
        blockable_count=1,
        origin_class="first_party",
        db=db,
    )
    wrote_owner = await immunity_shadow.record_would_block(
        gate=gate,
        source_kind="test",
        source_ref="t",
        process="server",
        blockable_count=1,
        origin_class="owner",
        db=db,
    )
    assert wrote_fp is False and wrote_owner is False
    if mode == "off":
        assert wrote_ext is False
        assert await crud.count(db) == 0
    else:
        assert wrote_ext is True
        rows = await crud.list_recent(db)
        assert len(rows) == 1 and rows[0]["gate"] == gate
    await db.close()


@pytest.mark.asyncio
async def test_gate3_crud_choke_emits_end_to_end(tmp_path):
    """Drive the REAL mutators: a synthetic external-origin evidence write →
    one gate=autonomy row with the cell key in detail; the owner/first-party
    paths (every live caller today) → zero rows."""
    from genesis.autonomy.types import CellEvent

    db = await _migrated(tmp_path / "g.db")
    ts = "2026-07-12T00:00:00+00:00"
    cell = dict(domain="email", verb="send", risk_class="standard")

    # First-party CLASSIFY (the live email-gate path) → no row.
    await cg.apply_event(
        db,
        **cell,
        event=CellEvent.CLASSIFY,
        updated_at=ts,
        origin_class="first_party",
    )
    # Owner-approved success (the live watcher path) → no row.
    await cg.record_success(db, **cell, updated_at=ts, origin_class="owner")
    assert await crud.count(db) == 0

    # Synthetic FUTURE external-influenced evidence → exactly one row.
    await cg.record_success(
        db,
        **cell,
        updated_at=ts,
        origin_class="external_untrusted",
    )
    rows = await crud.list_recent(db)
    assert len(rows) == 1
    assert rows[0]["gate"] == "autonomy"
    assert rows[0]["source_ref"] == "db/crud/capability_grants.py::record_success"
    assert "email:send:standard" in rows[0]["detail"]
    await db.close()


@pytest.mark.asyncio
async def test_gate3_kill_switch_off_records_nothing(tmp_path, monkeypatch):
    db = await _migrated(tmp_path / "g.db")
    monkeypatch.setattr(immunity, "gate_mode", lambda gate: "off")
    await cg.record_correction(
        db,
        domain="email",
        verb="send",
        risk_class="standard",
        updated_at="2026-07-12T00:00:00+00:00",
        origin_class="external_untrusted",
    )
    assert await crud.count(db) == 0
    await db.close()


def test_channel_origin_map_is_fail_closed():
    """Gate-2 steering origin: owner ALLOW-map only; voice and any unknown
    channel classify external_untrusted (fail-closed — the polarity fix for
    the fail-open _AUTONOMOUS_CHANNELS deny-list)."""
    from genesis.learning.pipeline import _CHANNEL_ORIGIN

    assert _CHANNEL_ORIGIN.get("telegram") == "owner"
    assert _CHANNEL_ORIGIN.get("terminal") == "owner"
    assert "voice" not in _CHANNEL_ORIGIN  # ambient multi-speaker STT
    assert _CHANNEL_ORIGIN.get("voice", "external_untrusted") == "external_untrusted"
    assert _CHANNEL_ORIGIN.get("some_future_channel", "external_untrusted") == (
        "external_untrusted"
    )
