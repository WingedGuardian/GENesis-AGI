"""Tests for dead-letter alert dedup + auto-resolution in the queues snapshot.

Guards two halves of the write-only-observation fix:
  - the writer dedups by count *band* (stable content_hash + skip_if_duplicate),
    so a spike that drifts (310 -> 319 -> 326) produces ONE unresolved row, not N;
  - the queue draining resolves outstanding infrastructure_alert observations,
    so stale "DLQ at N" alerts don't linger until TTL and poison the report.
"""

import importlib

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables

# The snapshots package re-exports the ``queues`` function, which shadows the
# submodule on attribute access — import the module object explicitly so we can
# reach the helpers + the module-global cooldown.
q = importlib.import_module("genesis.observability.snapshots.queues")


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture(autouse=True)
def _reset_cooldown():
    # The cooldown + last-band are process-globals; reset them around every test
    # so each exercises the intended path, not leftover state.
    q._last_dead_letter_alert_at = 0.0
    q._last_dead_letter_band = ""
    yield
    q._last_dead_letter_alert_at = 0.0
    q._last_dead_letter_band = ""


async def _unresolved_infra(db) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM observations WHERE source='dead_letter_monitor' "
        "AND type='infrastructure_alert' AND resolved=0"
    )
    return rows[0][0]


async def _total_infra(db) -> int:
    rows = await db.execute_fetchall(
        "SELECT COUNT(*) FROM observations WHERE source='dead_letter_monitor'"
    )
    return rows[0][0]


def test_dlq_band_buckets():
    assert q._dlq_band(50) == "50-99"
    assert q._dlq_band(99) == "50-99"
    assert q._dlq_band(150) == "100-199"
    assert q._dlq_band(319) == "200-499"
    assert q._dlq_band(900) == "500+"


@pytest.mark.asyncio
async def test_count_drift_in_same_band_dedups(db):
    """310 -> 319 -> 326 all fall in the 200-499 band -> exactly one row."""
    for count in (310, 319, 326):
        q._last_dead_letter_alert_at = 0.0  # bypass the cooldown for the test
        await q._alert_dead_letter_accumulation(db, count)
    assert await _unresolved_infra(db) == 1


@pytest.mark.asyncio
async def test_band_crossing_creates_new_alert(db):
    """A worsening that crosses a band boundary IS a new, distinct alert."""
    q._last_dead_letter_alert_at = 0.0
    await q._alert_dead_letter_accumulation(db, 60)  # 50-99
    q._last_dead_letter_alert_at = 0.0
    await q._alert_dead_letter_accumulation(db, 250)  # 200-499
    assert await _unresolved_infra(db) == 2


@pytest.mark.asyncio
async def test_band_escalation_within_cooldown_still_alerts(db):
    """A worsening that crosses a band boundary must NOT be swallowed by the 1h
    cooldown — band changes bypass it. (The cooldown is NOT reset between calls.)"""
    q._last_dead_letter_alert_at = 0.0
    q._last_dead_letter_band = ""
    await q._alert_dead_letter_accumulation(db, 60)  # 50-99; arms the cooldown
    # Cooldown is now active. A worsening into a new band must still alert...
    await q._alert_dead_letter_accumulation(db, 300)  # 200-499; band changed
    assert await _unresolved_infra(db) == 2
    # ...but a same-band tick while the cooldown is active does NOT add a row.
    await q._alert_dead_letter_accumulation(db, 320)  # still 200-499
    assert await _unresolved_infra(db) == 2


@pytest.mark.asyncio
async def test_resolve_on_drain(db):
    q._last_dead_letter_alert_at = 0.0
    await q._alert_dead_letter_accumulation(db, 319)
    assert await _unresolved_infra(db) == 1
    await q._resolve_dead_letter_alerts(db, 0)
    assert await _unresolved_infra(db) == 0


@pytest.mark.asyncio
async def test_resolve_then_respike_realerts(db):
    """After drain+resolve, a fresh spike in the same band creates a new row
    (skip_if_duplicate only suppresses UNRESOLVED duplicates)."""
    q._last_dead_letter_alert_at = 0.0
    await q._alert_dead_letter_accumulation(db, 319)
    await q._resolve_dead_letter_alerts(db, 0)  # resolves + resets cooldown to 0
    await q._alert_dead_letter_accumulation(db, 330)  # same band, prior is resolved
    assert await _unresolved_infra(db) == 1
    assert await _total_infra(db) == 2


class _DLQ:
    """Fake DeadLetterQueue: raw pending total + genuinely-stuck subset.

    ``stuck`` defaults to ``n`` (all pending are stuck) so a genuine backlog still
    alerts; pass a smaller ``stuck`` to model a self-healing burst (high raw total,
    few/zero stuck) that must NOT cry wolf.
    """

    def __init__(self, n: int, stuck: int | None = None) -> None:
        self._n = n
        self._stuck = n if stuck is None else stuck

    async def get_pending_count(self) -> int:
        return self._n

    async def get_stuck_count(self) -> int:
        return self._stuck


@pytest.mark.asyncio
async def test_queues_end_to_end_spike_then_drain(db):
    """queues() alerts on a genuine (stuck) spike and resolves on drain."""
    q._last_dead_letter_alert_at = 0.0
    await q.queues(db, None, _DLQ(200), None)  # 200 stuck >= threshold
    assert await _unresolved_infra(db) == 1

    await q.queues(db, None, _DLQ(0), None)  # drained < threshold
    assert await _unresolved_infra(db) == 0


@pytest.mark.asyncio
async def test_queues_high_pending_low_stuck_does_not_alert(db):
    """The cry-wolf fix: a big raw pending total that is all self-healing (0 stuck,
    e.g. a fresh chain_exhausted:judge burst) must NOT alert — but the snapshot
    still reports the honest raw total."""
    q._last_dead_letter_alert_at = 0.0
    result = await q.queues(db, None, _DLQ(200, stuck=0), None)
    assert await _unresolved_infra(db) == 0        # no cry-wolf
    assert result["dead_letters"] == 200           # raw total stays honest
