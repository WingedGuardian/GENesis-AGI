"""The actual launcher binds its ordinary snapshot before returning its owner."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import genesis.caller_context as identity
import genesis.caller_credentials as credentials
import genesis.env as environment
from genesis.env import genesis_db_path as real_db_path
from genesis.mcp import shared_settings
from genesis.mcp.database_target import database_target
from genesis.mcp.shared_client import _stdio
from tests.test_mcp.test_database_target_client import target_client as target_client


@pytest.mark.parametrize("status_mode", [
    "snapshot", "later-env", "http-error", "invalid-json", "null-status", "missing-catalog",
])
def test_actual_launcher_keeps_ordinary_snapshot_and_declines_before_preparation(
    target_client, monkeypatch, tmp_path, status_mode,
):
    state = target_client
    later = tmp_path / "later-env.db"
    later.touch()
    # Capture the genuine resolver at collection, before the suite's autouse
    # live-store fence replaces it; restore only for this owned path.
    monkeypatch.setenv("GENESIS_DB_PATH", str(state.path))
    assert real_db_path() == state.path and state.path.is_relative_to(tmp_path)
    monkeypatch.setattr(environment, "genesis_db_path", real_db_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    launcher = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts/genesis_mcp_server.py"))
    select = launcher["_local_frontend"]
    assert select.__globals__["_DEFAULT_DB"] == state.path
    monkeypatch.setenv("GENESIS_DB_PATH", str(later))

    def forbidden(*args, **kwargs):
        pytest.fail("selection must not prepare memory or claim input")

    monkeypatch.setitem(select.__globals__, "_prepare_memory", forbidden)
    monkeypatch.setattr(_stdio, "_claim_fd", forbidden)
    monkeypatch.setattr(shared_settings, "load_local_settings", lambda *args: SimpleNamespace(
        settings=state.settings, stage="memory-floor"))
    monkeypatch.setattr(identity, "caller_context_from_env", lambda: SimpleNamespace(profile=None))
    monkeypatch.setattr(credentials, "forwarder_headers", lambda caller: {"Authorization": "Bearer fixture"})
    if status_mode == "later-env":
        state.status["database_target_contract"]["target"] = database_target(later)
    elif status_mode == "http-error":
        state.code = 503
    elif status_mode == "invalid-json":
        state.body = b"{bad"
    elif status_mode == "null-status":
        state.status = None
    elif status_mode == "missing-catalog":
        del state.status["catalog_revision"]
    selected = select(SimpleNamespace(server="memory", transport="stdio", external_client=None), ["--server", "memory"])
    assert callable(selected) is (status_mode == "snapshot")
    assert select.__globals__["_DEFAULT_DB"] == state.path
    assert state.entered == state.closed == len(state.requests) == 1
