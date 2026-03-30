"""Tests for genesis.autonomy.state_machine.AutonomyManager."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
import yaml

from genesis.autonomy.state_machine import AutonomyManager
from genesis.autonomy.types import (
    AutonomyCategory,
    AutonomyLevel,
    AutonomyState,
)
from genesis.db.schema import create_all_tables

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
def config_file(tmp_path):
    cfg = tmp_path / "autonomy.yaml"
    cfg.write_text(
        yaml.dump(
            {
                "defaults": {
                    "direct_session": 1,
                    "background_cognitive": 1,
                    "sub_agent": 1,
                    "outreach": 1,
                },
                "ceilings": {
                    "direct_session": 7,
                    "background_cognitive": 3,
                    "sub_agent": 2,
                    "outreach": 2,
                },
            }
        )
    )
    return cfg


@pytest.fixture
def manager(db, config_file):
    return AutonomyManager(db=db, config_path=config_file)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# TestLoadOrCreateDefaults
# ---------------------------------------------------------------------------


class TestLoadOrCreateDefaults:
    @pytest.mark.asyncio
    async def test_creates_all_categories_on_empty_db(self, manager):
        states = await manager.load_or_create_defaults()
        assert set(states.keys()) == {c.value for c in AutonomyCategory}
        for cat in AutonomyCategory:
            assert cat.value in states
            assert isinstance(states[cat.value], AutonomyState)

    @pytest.mark.asyncio
    async def test_idempotent_on_second_call(self, manager):
        states_first = await manager.load_or_create_defaults()
        states_second = await manager.load_or_create_defaults()
        # Same IDs — no duplicates created.
        for cat in AutonomyCategory:
            assert states_first[cat.value].id == states_second[cat.value].id

    @pytest.mark.asyncio
    async def test_uses_config_defaults(self, db, tmp_path):
        cfg = tmp_path / "custom.yaml"
        cfg.write_text(
            yaml.dump(
                {
                    "defaults": {
                        "direct_session": 2,
                        "background_cognitive": 2,
                        "sub_agent": 2,
                        "outreach": 2,
                    },
                }
            )
        )
        mgr = AutonomyManager(db=db, config_path=cfg)
        states = await mgr.load_or_create_defaults()
        for cat in AutonomyCategory:
            assert states[cat.value].current_level == AutonomyLevel.L2


# ---------------------------------------------------------------------------
# TestGetState
# ---------------------------------------------------------------------------


class TestGetState:
    @pytest.mark.asyncio
    async def test_returns_none_for_missing(self, manager):
        result = await manager.get_state("direct_session")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_state_after_init(self, manager):
        await manager.load_or_create_defaults()
        state = await manager.get_state("direct_session")
        assert state is not None
        assert isinstance(state, AutonomyState)
        assert state.category == AutonomyCategory.DIRECT_SESSION


# ---------------------------------------------------------------------------
# TestEffectiveLevel
# ---------------------------------------------------------------------------


class TestEffectiveLevel:
    @pytest.mark.asyncio
    async def test_respects_ceiling(self, manager):
        """background_cognitive ceiling is 3; set level to 4 -> effective is 3."""
        await manager.load_or_create_defaults()
        await manager.set_level("background_cognitive", 4)
        effective = await manager.effective_level("background_cognitive")
        assert effective == 3

    @pytest.mark.asyncio
    async def test_direct_session_no_ceiling(self, manager):
        """direct_session ceiling is 7, so level 4 -> effective 4."""
        await manager.load_or_create_defaults()
        await manager.set_level("direct_session", 4)
        effective = await manager.effective_level("direct_session")
        assert effective == 4

    @pytest.mark.asyncio
    async def test_returns_zero_for_missing(self, manager):
        effective = await manager.effective_level("nonexistent_category")
        assert effective == 0


# ---------------------------------------------------------------------------
# TestSetLevel
# ---------------------------------------------------------------------------


class TestSetLevel:
    @pytest.mark.asyncio
    async def test_set_level_success(self, manager):
        await manager.load_or_create_defaults()
        ok = await manager.set_level("direct_session", 2)
        assert ok is True
        state = await manager.get_state("direct_session")
        assert state is not None
        assert state.current_level == AutonomyLevel.L2

    @pytest.mark.asyncio
    async def test_rejects_out_of_range(self, manager):
        await manager.load_or_create_defaults()
        assert await manager.set_level("direct_session", 0) is False
        assert await manager.set_level("direct_session", 5) is False

    @pytest.mark.asyncio
    async def test_earned_ratchets_up(self, manager):
        """Set to L3 then L2 — earned should stay at L3."""
        await manager.load_or_create_defaults()
        await manager.set_level("direct_session", 3)
        await manager.set_level("direct_session", 2)
        state = await manager.get_state("direct_session")
        assert state is not None
        assert state.current_level == AutonomyLevel.L2
        assert state.earned_level == AutonomyLevel.L3


# ---------------------------------------------------------------------------
# TestRestoreEarnedLevel
# ---------------------------------------------------------------------------


class TestRestoreEarnedLevel:
    @pytest.mark.asyncio
    async def test_restore_after_regression(self, manager):
        """Set L3, regression drops to L2, restore -> back to L3."""
        await manager.load_or_create_defaults()
        await manager.set_level("direct_session", 3)
        # Simulate regression by setting level lower directly.
        await manager.set_level("direct_session", 2)
        state = await manager.get_state("direct_session")
        assert state is not None
        assert state.current_level == AutonomyLevel.L2
        assert state.earned_level == AutonomyLevel.L3

        ok = await manager.restore_earned_level("direct_session")
        assert ok is True
        restored = await manager.get_state("direct_session")
        assert restored is not None
        assert restored.current_level == AutonomyLevel.L3


# ---------------------------------------------------------------------------
# TestRecordCorrection
# ---------------------------------------------------------------------------


class TestRecordCorrection:
    @pytest.mark.asyncio
    async def test_bayesian_correction_with_successes_no_regression(self, manager):
        """With enough successes, a single correction doesn't cause regression."""
        await manager.load_or_create_defaults()
        await manager.set_level("direct_session", 3)
        # Build up 10 successes (10S+0C → posterior 0.85 → L4, promoted incrementally)
        for _ in range(10):
            await manager.record_success("direct_session")
        state_before = await manager.get_state("direct_session")
        level_before = state_before.current_level
        success, regressed = await manager.record_correction(
            "direct_session", corrected_at=_now_iso()
        )
        assert success is True
        assert regressed is False  # 10S + 1C → posterior 0.85 → stays at current level
        state = await manager.get_state("direct_session")
        assert state is not None
        assert state.current_level == level_before  # unchanged

    @pytest.mark.asyncio
    async def test_bayesian_correction_no_successes_regresses(self, manager):
        """Without successes, first correction causes Bayesian regression."""
        await manager.load_or_create_defaults()
        await manager.set_level("direct_session", 3)
        success, regressed = await manager.record_correction(
            "direct_session", corrected_at=_now_iso()
        )
        assert success is True
        assert regressed is True  # 0S + 1C → posterior 0.33 → L2 < L3
        state = await manager.get_state("direct_session")
        assert state is not None
        assert state.current_level == AutonomyLevel.L2

    @pytest.mark.asyncio
    async def test_regression_emits_event(self, db, config_file):
        event_bus = MagicMock()
        event_bus.emit = AsyncMock()
        mgr = AutonomyManager(db=db, event_bus=event_bus, config_path=config_file)
        await mgr.load_or_create_defaults()
        await mgr.set_level("direct_session", 3)
        # With Bayesian regression, first correction (0S+1C) already triggers
        # regression (posterior=0.33 → L2)
        await mgr.record_correction("direct_session", corrected_at=_now_iso())
        await asyncio.sleep(0)
        event_bus.emit.assert_called_once()
        args = event_bus.emit.call_args
        assert args[0][2] == "autonomy.regression"  # event_type is 3rd positional arg
        assert args[1]["category"] == "direct_session"  # category in kwargs


# ---------------------------------------------------------------------------
# TestRecordSuccess
# ---------------------------------------------------------------------------


class TestRecordSuccess:
    @pytest.mark.asyncio
    async def test_success_resets_consecutive(self, manager):
        await manager.load_or_create_defaults()
        await manager.set_level("direct_session", 3)
        # Build up successes first so 1 correction doesn't regress (keeps consecutive=1)
        for _ in range(10):
            await manager.record_success("direct_session")
        await manager.record_correction(
            "direct_session", corrected_at=_now_iso()
        )
        state_before = await manager.get_state("direct_session")
        assert state_before is not None
        assert state_before.consecutive_corrections == 1  # no regression, counter kept

        ok, _promoted = await manager.record_success("direct_session")
        assert ok is True
        state_after = await manager.get_state("direct_session")
        assert state_after is not None
        assert state_after.consecutive_corrections == 0

    @pytest.mark.asyncio
    async def test_success_increments_total(self, manager):
        await manager.load_or_create_defaults()
        state_before = await manager.get_state("direct_session")
        assert state_before is not None
        initial = state_before.total_successes

        await manager.record_success("direct_session")
        state_after = await manager.get_state("direct_session")
        assert state_after is not None
        assert state_after.total_successes == initial + 1


# ---------------------------------------------------------------------------
# TestCheckCeiling
# ---------------------------------------------------------------------------


class TestCheckCeiling:
    def test_ceiling_check_passes(self, manager):
        """background_cognitive ceiling is 3, required L2 -> True."""
        assert manager.check_ceiling("background_cognitive", 2) is True

    def test_ceiling_check_fails(self, manager):
        """outreach ceiling is 2, required L3 -> False."""
        assert manager.check_ceiling("outreach", 3) is False
