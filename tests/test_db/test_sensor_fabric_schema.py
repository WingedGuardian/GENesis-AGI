"""WS-2 M9/M10 base-schema parity — the sensor tables + their indexes exist
when the schema is built from TABLES/INDEXES (create_all_tables), not just via
the versioned migration.

Regression lock for the gap Codex caught on #1077: the partial unique index
`idx_ae_open_alert` is what makes `alert_events` reconcile idempotent, and it
must be present on EVERY schema-build path — a fresh install goes through
create_all_tables(), so an index that lives only in the migration would leave
fresh installs accumulating duplicate open incident rows every tick.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud import alert_events as ae
from genesis.db.schema import create_all_tables


async def _built_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await create_all_tables(db)
    return db


async def _index_names(db) -> set[str]:
    async with db.execute("SELECT name FROM sqlite_master WHERE type='index'") as cur:
        return {r[0] for r in await cur.fetchall()}


async def test_sensor_tables_and_indexes_in_base_schema():
    db = await _built_db()
    try:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('job_run_events', 'alert_events')"
        ) as cur:
            tables = {r[0] for r in await cur.fetchall()}
        assert tables == {"job_run_events", "alert_events"}

        idx = await _index_names(db)
        # The load-bearing one: without this the open-set reconcile is not idempotent.
        assert "idx_ae_open_alert" in idx
        assert {"idx_ae_created", "idx_ae_alert"} <= idx
        assert {"idx_jre_job_time", "idx_jre_recorded", "idx_jre_status"} <= idx
    finally:
        await db.close()


async def test_open_set_idempotent_on_base_schema():
    """The partial unique index must actually dedup on a base-schema-built DB."""
    db = await _built_db()
    try:
        alert = [{"alert_id": "e2e:probe", "source": "e2e", "severity": "CRITICAL", "message": "x"}]
        await ae.reconcile_open_set(db, active=alert, now="2026-07-15T00:00:00")
        await ae.reconcile_open_set(db, active=alert, now="2026-07-15T00:05:00")
        assert len(await ae.list_open(db)) == 1, "repeat firing must not open a 2nd row"
    finally:
        await db.close()


pytestmark = pytest.mark.asyncio
