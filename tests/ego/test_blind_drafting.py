"""PR-5 unit C — blind drafting: when the reconcile stage is active (shadow/live)
the pending board is withheld from drafting context, but the Recently-Tried
learning signal stays. Off = today's behavior."""

from __future__ import annotations

from datetime import UTC, datetime

import aiosqlite
import pytest

from genesis.db.crud import ego as ego_crud
from genesis.db.schema import TABLES
from genesis.ego import reconcile_config
from genesis.ego.genesis_context import GenesisEgoContextBuilder
from genesis.ego.user_context import UserEgoContextBuilder


@pytest.fixture
async def db():
    async with aiosqlite.connect(":memory:") as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(TABLES["ego_proposals"])
        yield conn


async def _seed_pair(db, ego_source: str):
    now = datetime.now(UTC).isoformat()
    await ego_crud.create_proposal(
        db,
        id=f"{ego_source}-pending",
        action_type="investigate",
        content="PENDING_MARKER an active proposal",
        rationale="r",
        status="pending",
        ego_source=ego_source,
        created_at=now,
    )
    await ego_crud.create_proposal(
        db,
        id=f"{ego_source}-tried",
        action_type="outreach",
        content="TRIED_MARKER a withdrawn proposal",
        rationale="r",
        status="withdrawn",
        ego_source=ego_source,
        created_at=now,
    )


# ── _proposal_history_section: pending half withheld only when blind ──────


@pytest.mark.parametrize(
    ("builder_cls", "ego_source"),
    [
        (GenesisEgoContextBuilder, "genesis_ego_cycle"),
        (UserEgoContextBuilder, "user_ego_cycle"),
    ],
)
async def test_history_not_blind_shows_active(db, builder_cls, ego_source):
    await _seed_pair(db, ego_source)
    builder = builder_cls(db=db)
    builder._blind_drafting = False
    out = await builder._proposal_history_section()
    assert "## Active Proposals" in out
    assert "PENDING_MARKER" in out
    assert "Recently Tried" in out
    assert "TRIED_MARKER" in out


@pytest.mark.parametrize(
    ("builder_cls", "ego_source"),
    [
        (GenesisEgoContextBuilder, "genesis_ego_cycle"),
        (UserEgoContextBuilder, "user_ego_cycle"),
    ],
)
async def test_history_blind_withholds_active_keeps_tried(db, builder_cls, ego_source):
    await _seed_pair(db, ego_source)
    builder = builder_cls(db=db)
    builder._blind_drafting = True
    out = await builder._proposal_history_section()
    assert "## Active Proposals" not in out
    assert "PENDING_MARKER" not in out
    # The learning signal survives.
    assert "Recently Tried" in out
    assert "TRIED_MARKER" in out


async def test_history_light_branch_blind_hides_active_count(db):
    """The light-depth user summary drops the Active count when blind."""
    await _seed_pair(db, "user_ego_cycle")
    builder = UserEgoContextBuilder(db=db)
    builder._blind_drafting = True
    out = await builder._proposal_history_section(depth="light")
    assert "Active:" not in out
    assert "Recently tried:" in out


# ── build(): proposal_board section dropped from the section_map when blind ─


def _stub_all_sections(builder):
    """Replace every _*_section method with a marker-returning async stub so
    build() needs no live services. Markers are keyed by method name."""

    def make_stub(name):
        async def _stub(*, depth: str = "deep") -> str:
            return f"[[{name}]]"

        return _stub

    for attr in dir(builder):
        if attr.endswith("_section"):
            name = attr[1 : -len("_section")]
            setattr(builder, attr, make_stub(name))


async def test_build_excludes_board_when_blind(db, monkeypatch):
    monkeypatch.setattr(reconcile_config, "effective_mode", lambda: "shadow")
    builder = GenesisEgoContextBuilder(db=db)
    _stub_all_sections(builder)
    out = await builder.build()
    assert "[[proposal_board]]" not in out
    assert "[[proposal_history]]" in out  # history stays (its own half-gating is separate)


async def test_build_keeps_board_when_off(db, monkeypatch):
    monkeypatch.setattr(reconcile_config, "effective_mode", lambda: "off")
    builder = GenesisEgoContextBuilder(db=db)
    _stub_all_sections(builder)
    out = await builder.build()
    assert "[[proposal_board]]" in out
    assert "[[proposal_history]]" in out
