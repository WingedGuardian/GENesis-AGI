"""Tests for calibration feedback injection in ContextAssembler.

WS-2 P3 repointed `_build_calibration_text` from the legacy
``calibration_curves`` to ``calibration_cells``. The rendered-sentence
contract is pinned by the parity test below (header + line shape byte-stable
across the repoint); the new source semantics under test: stated lane only,
ok-status only (thin/unknown never render on this surface), ``ego``/``ego.*``
excluded, 90d window preferred with all-time fallback per (domain, class,
metric). Tests must NOT assert cross-pass stability of the chosen window —
a domain flipping 90d↔all-time between passes is by design.
"""

from __future__ import annotations

import re

import pytest

from genesis.awareness.types import Depth, SignalReading, TickResult
from genesis.identity.loader import IdentityLoader
from genesis.perception.context import ContextAssembler

_LINE_RE = re.compile(
    r"  - When you report ~\d+% confidence, "
    r"you're historically right ~\d+% of the time \(n=\d+\)"
)


def _make_tick() -> TickResult:
    return TickResult(
        tick_id="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        timestamp="2026-03-14T12:00:00+00:00",
        source="test",
        classified_depth=Depth.DEEP,
        signals=[
            SignalReading(
                name="test_signal",
                value=0.5,
                source="test",
                collected_at="2026-03-14T12:00:00+00:00",
            )
        ],
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


async def _seed_cell(
    db,
    *,
    domain: str,
    mean_confidence: float = 0.8,
    shrunk_estimate: float = 0.6,
    n: int = 40,
    status: str = "ok",
    provenance: str = "stated",
    window_days: int = 90,
    action_class: str = "outreach_send",
    metric: str = "reply_received",
):
    await db.execute(
        "INSERT INTO calibration_cells (domain, action_class, metric, provenance,"
        " window_days, n, n_mechanical, base_rate, mean_confidence, shrunk_estimate,"
        " status, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
        " '2026-07-19T12:00:00+00:00')",
        (
            domain,
            action_class,
            metric,
            provenance,
            window_days,
            n,
            n,
            shrunk_estimate,
            mean_confidence,
            shrunk_estimate,
            status,
        ),
    )
    await db.commit()


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
        """Deep depth but no calibration data → None (fresh-install state)."""
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is None

    @pytest.mark.asyncio
    async def test_calibration_injected_at_deep(self, assembler, db):
        """Deep depth with an ok stated cell → text present, shrunk value shown."""
        await _seed_cell(db, domain="outreach", mean_confidence=0.8, shrunk_estimate=0.6, n=35)
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is not None
        assert "80%" in ctx.calibration_text
        assert "60%" in ctx.calibration_text
        assert "n=35" in ctx.calibration_text

    @pytest.mark.asyncio
    async def test_calibration_injected_at_strategic(self, assembler, db):
        """Strategic depth also gets calibration."""
        await _seed_cell(db, domain="routing", mean_confidence=0.9, shrunk_estimate=0.85, n=40)
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.STRATEGIC, tick, db=db)
        assert ctx.calibration_text is not None
        assert "routing" in ctx.calibration_text.lower()

    @pytest.mark.asyncio
    async def test_thin_and_unknown_cells_never_render(self, assembler, db):
        """A thin cell would be a bare percentage here — it must not appear."""
        await _seed_cell(db, domain="task.deploy", n=14, status="thin")
        await _seed_cell(db, domain="build", n=3, status="unknown")
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is None

    @pytest.mark.asyncio
    async def test_skips_below_min_samples(self, assembler, db):
        """An ok-status cell below the assembler's min_samples → not injected."""
        await _seed_cell(db, domain="outreach", n=4, status="ok")
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is None

    @pytest.mark.asyncio
    async def test_ego_domains_excluded(self, assembler, db):
        """ego and ego.* stay scoped to the ego context (design §4.2)."""
        await _seed_cell(
            db, domain="ego.notify", action_class="ego_proposal", metric="approved_and_executes"
        )
        await _seed_cell(
            db, domain="ego", action_class="ego_proposal", metric="approved_and_executes"
        )
        await _seed_cell(db, domain="outreach")
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is not None
        assert "ego" not in ctx.calibration_text
        assert "outreach" in ctx.calibration_text

    @pytest.mark.asyncio
    async def test_policy_prior_cells_never_render(self, assembler, db):
        """A prior is not 'you reported' — prior-only data renders nothing."""
        await _seed_cell(db, domain="outreach", provenance="policy_prior")
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is None

    @pytest.mark.asyncio
    async def test_prefers_90d_falls_back_to_all_time(self, assembler, db):
        """90d cell wins where present; all-time fills in where it isn't."""
        await _seed_cell(
            db, domain="outreach", window_days=90, n=35, mean_confidence=0.8, shrunk_estimate=0.7
        )
        await _seed_cell(
            db, domain="outreach", window_days=0, n=200, mean_confidence=0.5, shrunk_estimate=0.4
        )
        await _seed_cell(
            db, domain="routing", window_days=0, n=50, mean_confidence=0.9, shrunk_estimate=0.88
        )
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is not None
        assert "n=35" in ctx.calibration_text  # the 90d cell, not n=200
        assert "n=200" not in ctx.calibration_text
        assert "n=50" in ctx.calibration_text  # routing's all-time fallback

    @pytest.mark.asyncio
    async def test_multiple_domains(self, assembler, db):
        """Multiple domains with sufficient data → all included."""
        await _seed_cell(db, domain="outreach")
        await _seed_cell(db, domain="routing")
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        assert ctx.calibration_text is not None
        assert "outreach" in ctx.calibration_text.lower()
        assert "routing" in ctx.calibration_text.lower()

    @pytest.mark.asyncio
    async def test_rendered_contract_parity(self, assembler, db):
        """The pre-repoint rendering contract, byte-stable: header + line shape."""
        await _seed_cell(db, domain="outreach", mean_confidence=0.8, shrunk_estimate=0.6, n=35)
        await _seed_cell(db, domain="routing", mean_confidence=0.9, shrunk_estimate=0.85, n=40)
        tick = _make_tick()
        ctx = await assembler.assemble(Depth.DEEP, tick, db=db)
        lines = ctx.calibration_text.split("\n")
        assert lines[0] == "Historical calibration (adjust your confidence accordingly):"
        body = lines[1:]
        domain_headers = [ln for ln in body if ln.startswith("Domain: ")]
        stat_lines = [ln for ln in body if not ln.startswith("Domain: ")]
        assert domain_headers == ["Domain: outreach", "Domain: routing"]
        assert stat_lines and all(_LINE_RE.fullmatch(ln) for ln in stat_lines)

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
