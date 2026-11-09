"""Actual CLI refuses a missing versioned route before effects or fallback."""

from tests.shared_mcp_qualification import QUALIFIED_TEST_STACK as QUALIFIED_TEST_STACK

# isort: split
import asyncio
import signal

import pytest

from tests.test_mcp.test_shared_cli_startup_contract import shared_install as shared_install
from tests.test_mcp.test_shared_forwarder import (
    LEGACY,
    MODERN,
    call,
    effects,
    initialize,
    receive,
    send,
    synthetic,
)


@pytest.mark.parametrize("service", ["health"])
@pytest.mark.parametrize("era", [MODERN, LEGACY])
@pytest.mark.parametrize("rollback", [False, True], ids=["older-route-at-start", "rollback-open-client"])
async def test_missing_version_route_never_dispatches_or_downgrades(tmp_path, era, rollback, shared_install):
    async with synthetic(tmp_path, shared_install) as (start_backend, start_frontend):
        old_route = tmp_path / "fixture-old-route"
        if not rollback:
            old_route.touch()
        backend = await start_backend()
        frontend = await start_frontend()
        if rollback:
            await initialize(frontend, era)
            await send(frontend, call("before", "before", era))
            assert (await receive(frontend))["result"]["structuredContent"]["marker"] == "before"
            backend.send_signal(signal.SIGTERM)
            assert await asyncio.wait_for(backend.wait(), 30) == -signal.SIGTERM
            assert any(row["event"] == "owner-closed" for row in effects(tmp_path))
            old_route.touch()
            await start_backend()  # Its original /mcp oracle still executes.
        elif era == LEGACY:
            await send(frontend, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
                "protocolVersion": era, "capabilities": {}, "clientInfo": {"name": "fixture", "version": "1"}}})
            refused = await receive(frontend)
            assert refused["id"] == 0 and type(refused["id"]) is int and "error" in refused
        for identifier in (7, "later"):
            await send(frontend, call(identifier, "must-not-run", era))
            refused = await receive(frontend)
            assert type(refused["id"]) is type(identifier) and refused["id"] == identifier
            assert "error" in refused, refused
        frontend.stdin.close()
        assert await asyncio.wait_for(frontend.wait(), 30) == 0
    # Read after all owners settle, including any late resend.
    rows = effects(tmp_path)
    assert not [row for row in rows if row["event"] == "entered" and row.get("marker") == "must-not-run"]
    assert sum(row["event"] == "entered" and row.get("marker") == "before" for row in rows) == int(rollback)
    assert sum(row["event"] == "entered" and row.get("marker") == "seed" for row in rows) == 1 + int(rollback)
