"""Tests for cell_promotion (WS-8 PR-D): the shared resolution hook that promotes
an email capability cell ASK→GRANTED on the owner's approval, plus guards that
the hook is wired into every resolution entry point and excluded from the
approved-proposal dispatch sweep."""

from __future__ import annotations

import json
from pathlib import Path

import aiosqlite
import pytest

import genesis
from genesis.autonomy.types import CellEvent, CellState
from genesis.db.crud import capability_grants as cg
from genesis.db.crud import ego as ego_crud
from genesis.db.schema import create_all_tables
from genesis.ego.cell_promotion import _parse_cell, handle_cell_promotion_resolution

_TS = "2026-06-21T00:00:00"
_CELL = {"domain": "email", "verb": "send", "risk_class": "standard"}


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


async def _promotable_cell(db, successes=5):
    """An ASK cell with enough approved successes to be promotable."""
    await cg.apply_event(db, event=CellEvent.CLASSIFY, updated_at=_TS, **_CELL)
    for _ in range(successes):
        await cg.record_success(db, updated_at=_TS, **_CELL)


async def _make_proposal(db, *, status="approved", action_type="cell_promotion",
                         cell=("email", "send", "standard")):
    pid = "cp1"
    await ego_crud.create_proposal(
        db, id=pid, action_type=action_type, action_category=":".join(cell),
        content="promote", status="pending",
        created_at="2026-06-20T00:00:00+00:00",
        expected_outputs=json.dumps({"cell": list(cell)}),
    )
    await ego_crud.resolve_proposal(db, pid, status=status)
    return await ego_crud.get_proposal(db, pid)


# ── _parse_cell ───────────────────────────────────────────────────────────


def test_parse_cell():
    assert _parse_cell('{"cell": ["email", "send", "standard"]}') == ("email", "send", "standard")
    assert _parse_cell({"cell": ["email", "send", "bulk"]}) == ("email", "send", "bulk")
    assert _parse_cell("not json") is None
    assert _parse_cell({"cell": ["only", "two"]}) is None
    assert _parse_cell(None) is None


# ── handle_cell_promotion_resolution ──────────────────────────────────────


async def test_approved_promotes_ask_cell(db):
    await _promotable_cell(db)
    prop = await _make_proposal(db, status="approved")

    ok = await handle_cell_promotion_resolution(db, prop, "approved")

    assert ok is True
    assert (await cg.get_cell(db, **_CELL))["state"] == CellState.GRANTED.value
    assert (await ego_crud.get_proposal(db, prop["id"]))["status"] == "executed"


async def test_rejected_sets_cooldown_no_promote(db):
    await _promotable_cell(db)
    prop = await _make_proposal(db, status="rejected")

    ok = await handle_cell_promotion_resolution(db, prop, "rejected")

    assert ok is False
    assert (await cg.get_cell(db, **_CELL))["state"] == CellState.ASK.value
    assert await ego_crud.get_state(
        db, "cell_promotion_reject:email:send:standard",
    ) is not None


async def test_non_cell_proposal_is_noop(db):
    await _promotable_cell(db)
    prop = await _make_proposal(db, status="approved", action_type="autonomy_earnback")

    ok = await handle_cell_promotion_resolution(db, prop, "approved")

    assert ok is False
    assert (await cg.get_cell(db, **_CELL))["state"] == CellState.ASK.value


async def test_approve_skips_when_evidence_changed(db):
    # Too few successes → not promotable → approval must NOT promote (the
    # staleness guard: a correction may have landed since the proposal was shown).
    await _promotable_cell(db, successes=2)
    prop = await _make_proposal(db, status="approved")

    ok = await handle_cell_promotion_resolution(db, prop, "approved")

    assert ok is False
    assert (await cg.get_cell(db, **_CELL))["state"] == CellState.ASK.value
    # still marked executed so it doesn't linger / get dispatched
    assert (await ego_crud.get_proposal(db, prop["id"]))["status"] == "executed"


# ── Wiring guards (built ≠ wired) ─────────────────────────────────────────


def test_handler_wired_into_all_resolution_paths():
    """Every proposal-resolution entry point MUST call the cell-promotion hook,
    or an approval there silently no-ops."""
    root = Path(genesis.__file__).parent
    for path in [
        root / "ego" / "proposals.py",
        root / "mcp" / "health" / "ego_tools.py",
        root / "dashboard" / "routes" / "ego.py",
        root / "dashboard" / "routes" / "comms.py",
    ]:
        src = path.read_text()
        assert "handle_cell_promotion_resolution" in src, (
            f"{path} is missing the cell-promotion apply hook — "
            "an approval there would silently no-op"
        )


def test_excluded_from_approved_proposal_sweep():
    """P1-A: cell_promotion must be in the sweep exclusion list, else an approved
    promotion gets dispatched as a CC session."""
    root = Path(genesis.__file__).parent
    src = (root / "ego" / "session.py").read_text()
    assert '"cell_promotion"' in src
