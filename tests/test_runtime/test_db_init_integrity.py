from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.db.integrity import DatabaseIntegrityError
from genesis.runtime import _capabilities
from genesis.runtime.init import db as db_init


@pytest.mark.asyncio
async def test_integrity_failure_prevents_init_and_migrations():
    runtime = MagicMock()
    runtime._db = None

    with (
        patch(
            "genesis.db.integrity.require_healthy_database",
            side_effect=DatabaseIntegrityError("malformed"),
        ),
        patch("genesis.db.connection.init_db", new_callable=AsyncMock) as init_db,
        patch("genesis.db.migrations.runner.MigrationRunner") as runner,
        pytest.raises(DatabaseIntegrityError),
    ):
        await db_init.init(runtime)

    assert runtime._db is None
    init_db.assert_not_awaited()
    runner.assert_not_called()


def test_failed_bootstrap_manifest_records_not_bootstrapped(tmp_path, monkeypatch):
    runtime = MagicMock()
    runtime._bootstrap_mode = "full"
    runtime._bootstrap_manifest = {"db": "failed: malformed"}
    runtime.is_bootstrapped = False
    monkeypatch.setattr(_capabilities.Path, "home", lambda: tmp_path)

    _capabilities.write_bootstrap_manifest_file(runtime)

    payload = json.loads((tmp_path / ".genesis" / "bootstrap_manifest.json").read_text())
    assert payload["bootstrapped"] is False
    assert payload["manifest"]["db"].startswith("failed:")
