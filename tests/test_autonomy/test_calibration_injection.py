"""Tests for calibration feedback injection in ContextAssembler."""

from __future__ import annotations

import pytest

from genesis.awareness.types import Depth, SignalReading, TickResult
from genesis.identity.loader import IdentityLoader
from genesis.perception.context import ContextAssembler


def _make_tick() -> TickResult:
    return TickResult(
        tick_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        timestamp="2026-03-14T12:00:00+00:00",
        source="test",
        classified_depth=Depth.DEEP,
        signals=[SignalReading(name="test_signal", value=0.5, source="test", collected_at="2026-03-14T12:00:00+00:00")],
        scores=[],
        trigger_reason=None,
    )


@pytest.fixture()
def assembler(tmp_path):
    # Create minimal identity files
    (tmp_path / "SOUL.md").write_text("I am Genesis.")
    (tmp_path / "USER.md").write_text("The user.")
    loader = IdentityLoader(identity_dir=tmp_path)
    return ContextAssembler(
        identity_loader=loader,
        calibration_min_samples=5,
    )


@pytest.fixture()
async def db(tmp_path):
    import aiosqlite

    from genesis.db.schema import create_all_tables

    db_path = tmp_path / "test.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


class TestCalibrationInjection:
    @pytest.mark.asyncio
    async def test_no_calibration_at_micro(self, assembler, db):
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.MICRO, tick, db=db)
        assert ctx.calibration_text is None

    @pytest.mark.asyncio
    async def test_no_calibration_at_light(self, assembler, db):
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)
        assert ctx.calibration_text is None

    @pytest.mark.asyncio
    async def test_no_calibration_when_empty(self, assembler, db):
        """Deep depth but no calibration data → None."""
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is None

    @pytest.mark.asyncio
    async def test_calibration_injected_at_deep(self, assembler, db):
        """Deep depth with sufficient calibration data → text present."""
        from genesis.db.crud.predictions import save_calibration_curve

        await save_calibration_curve(
            db,
            domain="outreach",
            confidence_bucket="0.8",
            predicted_confidence=0.8,
            actual_success_rate=0.6,
            sample_count=15,
            correction_factor=0.75,
        )
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is not None
        assert "80%" in ctx.calibration_text
        assert "60%" in ctx.calibration_text
        assert "n=15" in ctx.calibration_text

    @pytest.mark.asyncio
    async def test_calibration_injected_at_strategic(self, assembler, db):
        """Strategic depth also gets calibration."""
        from genesis.db.crud.predictions import save_calibration_curve

        await save_calibration_curve(
            db,
            domain="routing",
            confidence_bucket="0.9",
            predicted_confidence=0.9,
            actual_success_rate=0.85,
            sample_count=20,
            correction_factor=0.94,
        )
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.STRATEGIC, tick, db=db)
        assert ctx.calibration_text is not None
        assert "routing" in ctx.calibration_text.lower()

    @pytest.mark.asyncio
    async def test_skips_low_sample_count(self, assembler, db):
        """Calibration data below min_samples threshold → not injected."""
        from genesis.db.crud.predictions import save_calibration_curve

        await save_calibration_curve(
            db,
            domain="outreach",
            confidence_bucket="0.8",
            predicted_confidence=0.8,
            actual_success_rate=0.6,
            sample_count=3,  # Below threshold of 5
            correction_factor=0.75,
        )
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is None

    @pytest.mark.asyncio
    async def test_multiple_domains(self, assembler, db):
        """Multiple domains with sufficient data → all included."""
        from genesis.db.crud.predictions import save_calibration_curve

        await save_calibration_curve(
            db, domain="outreach", confidence_bucket="0.8",
            predicted_confidence=0.8, actual_success_rate=0.6,
            sample_count=10, correction_factor=0.75,
        )
        await save_calibration_curve(
            db, domain="routing", confidence_bucket="0.7",
            predicted_confidence=0.7, actual_success_rate=0.65,
            sample_count=12, correction_factor=0.93,
        )
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is not None
        assert "outreach" in ctx.calibration_text.lower()
        assert "routing" in ctx.calibration_text.lower()

    @pytest.mark.asyncio
    async def test_identity_block_includes_steering(self, assembler, db):
        """identity_block() now includes STEERING.md content."""
        # Write a STEERING.md file in the identity dir
        identity_dir = assembler._identity._dir
        steering = identity_dir / "STEERING.md"
        steering.write_text("# Steering Rules\n\n---\nNever do X\n")
        assembler._identity.reload()

        tick = _make_tick()
        ctx = await assembler.assemble(Depth.LIGHT, tick, db=db)
        assert "Never do X" in ctx.identity
