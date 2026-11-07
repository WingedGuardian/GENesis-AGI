"""Local readiness uses real disposable SQLite state, without enabling peers."""

from types import SimpleNamespace

import pytest
from flask import Flask

from genesis.peers.readiness import evaluate


@pytest.fixture
async def ready_app(registry, monkeypatch):
    from genesis.dashboard.routes.agent_api import agent_api_bp
    from genesis.db.connection import open_ro_connection
    from tests.test_peers.test_auth import prepare

    await prepare(registry, monkeypatch)
    db = await open_ro_connection(registry.path)
    runner = SimpleNamespace(_session_manager=SimpleNamespace(_db=db), closing=False)
    runtime = SimpleNamespace(_db=db, _direct_session_runner=runner)
    app = Flask(__name__)
    app.register_blueprint(agent_api_bp)
    app.config.update(GENESIS_PEER_REGISTRY=registry, GENESIS_PEER_RUNTIME=runtime)
    try:
        yield app
    finally:
        await db.close()


async def test_enabled_and_disabled(ready_app, registry):
    assert (await evaluate(ready_app))["status"] == "healthy"
    await registry.configure("disabled")
    ready_app.config["GENESIS_PEER_RUNTIME"]._direct_session_runner = None
    assert (await evaluate(ready_app))["status"] == "disabled"


@pytest.mark.parametrize("fault", ["closing", "runner", "runner_db", "registry_db", "routes"])
async def test_confirmed_runtime_faults(ready_app, registry, tmp_path, fault):
    runtime = ready_app.config["GENESIS_PEER_RUNTIME"]
    if fault == "closing":
        runtime._direct_session_runner.closing = True
    elif fault == "runner":
        runtime._direct_session_runner = None
    elif fault == "runner_db":
        runtime._direct_session_runner._session_manager._db = object()
    elif fault == "registry_db":
        import sqlite3

        from genesis.peers.registry import PeerRegistry

        # Use SQLite backup so the fixture includes committed WAL contents.
        with sqlite3.connect(registry.path) as source, sqlite3.connect(tmp_path / "other.db") as dest:
            source.backup(dest)
        ready_app.config["GENESIS_PEER_REGISTRY"] = PeerRegistry(tmp_path / "other.db")
    else:
        ready_app.blueprints.clear()
    assert (await evaluate(ready_app))["status"] == "degraded"


async def test_missing_database_is_unknown_without_creating_it(ready_app, tmp_path):
    from genesis.peers.registry import PeerRegistry

    path = tmp_path / "absent.db"
    ready_app.config["GENESIS_PEER_REGISTRY"] = PeerRegistry(path)
    assert (await evaluate(ready_app))["status"] == "unknown"
    assert not path.exists()


async def test_timeout_is_unknown(ready_app, monkeypatch):
    import asyncio

    async def stalled(*args):
        await asyncio.sleep(10)

    monkeypatch.setattr("genesis.peers.readiness._evaluate", stalled)
    result = await evaluate(ready_app)
    assert result["status"] == "unknown"
    assert result["reasons"] == ["check_timeout"]


async def test_read_only_setup_cancellation_closes_connection(monkeypatch, tmp_path):
    import asyncio
    import sqlite3

    import aiosqlite

    from genesis.db.connection import open_ro_connection

    path = tmp_path / "cancel.db"
    sqlite3.connect(path).close()
    connections = []
    original_connect = aiosqlite.connect

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connections.append(connection)
        return connection

    async def cancel_setup(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("genesis.db.connection.aiosqlite.connect", connect)
    monkeypatch.setattr(aiosqlite.Connection, "execute", cancel_setup)
    with pytest.raises(asyncio.CancelledError):
        await open_ro_connection(path)
    connection, = connections
    # Check both descriptor ownership and the actual worker, not just a close mock.
    if connection._connection is not None:
        await connection.close()
        pytest.fail("cancelled acquisition leaked its SQLite connection")
    worker = getattr(connection, "_thread", connection)
    worker.join(timeout=1)
    assert not worker.is_alive()


def test_disabled_capability_and_boot_manifest_ownership(tmp_path, monkeypatch):
    import json
    from pathlib import Path

    from genesis.runtime._capabilities import write_capabilities_file

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runtime = SimpleNamespace(_bootstrap_manifest={"agent_api": "disabled"},
                              _bootstrap_mode="full", is_bootstrapped=True, _module_registry=None)
    write_capabilities_file(runtime)
    assert json.loads((tmp_path / ".genesis/capabilities.json").read_text())[
        "agent_api"]["status"] == "disabled"
    manifest = tmp_path / ".genesis/bootstrap_manifest.json"
    original = manifest.read_bytes()
    runtime._bootstrap_mode = "readonly"
    write_capabilities_file(runtime)
    assert manifest.read_bytes() == original
