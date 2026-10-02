"""Activation-baseline pin: not-restarted rows still count as successful updates.

Per the owner ruling, a `genesis-server-not-restarted` success row moved the
checkout, ran migrations, and redeployed the host Guardian — so
`last_successful_deploy_commit` and `last_successful_update` must return it.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.db.crud.update_history import (
    last_successful_deploy_commit,
    last_successful_update,
)

_DDL = """
CREATE TABLE update_history (
    id TEXT PRIMARY KEY,
    old_tag TEXT, new_tag TEXT, old_commit TEXT, new_commit TEXT,
    status TEXT NOT NULL, rollback_tag TEXT, failure_reason TEXT,
    degraded_subsystems TEXT,
    started_at TEXT NOT NULL, completed_at TEXT
)
"""


async def _make_db(tmp_path, rows):
    db_path = tmp_path / "genesis.db"
    async with aiosqlite.connect(str(db_path)) as db:
        await db.execute(_DDL)
        for r in rows:
            await db.execute(
                "INSERT INTO update_history "
                "(id, old_tag, new_tag, old_commit, new_commit, status, "
                " rollback_tag, failure_reason, degraded_subsystems, "
                " started_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    r["id"], "v0.3.0", "v0.3.1", "old",
                    r["new_commit"], r["status"], None, None,
                    r.get("degraded_subsystems"),
                    r["started_at"], r["completed_at"],
                ),
            )
        await db.commit()
    return db_path


@pytest.mark.asyncio
async def test_not_restarted_success_is_the_activation_baseline(tmp_path):
    db_path = await _make_db(tmp_path, [
        {
            "id": "clean", "status": "success", "new_commit": "commit_old",
            "started_at": "2026-04-10T10:00:00+00:00",
            "completed_at": "2026-04-10T10:00:30+00:00",
        },
        {
            "id": "not-restarted", "status": "success",
            "new_commit": "commit_new",
            "degraded_subsystems": "genesis-server-not-restarted",
            "started_at": "2026-04-10T11:00:00+00:00",
            "completed_at": "2026-04-10T11:00:30+00:00",
        },
    ])
    async with aiosqlite.connect(str(db_path)) as db:
        assert await last_successful_deploy_commit(db) == "commit_new"
        assert await last_successful_update(db) == (
            "2026-04-10T11:00:30+00:00", "commit_new",
        )
