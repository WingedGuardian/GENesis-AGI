"""Exercise the two migrated async catalog consumers at their response boundary."""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from flask import Flask


@pytest.mark.parametrize("consumer", ["dashboard", "snapshot"])
@pytest.mark.parametrize("broken", [False, True])
async def test_catalog_consumers_preserve_names_counts_and_truthful_errors(consumer, broken, monkeypatch, tmp_path):
    from genesis.dashboard.routes import services as dashboard

    snapshot = importlib.import_module("genesis.observability.snapshots.services")
    monkeypatch.setattr(snapshot, "_MCP_CRASH_DIR", tmp_path / "no-crashes")
    module = dashboard if consumer == "dashboard" else snapshot
    if broken:
        fake = SimpleNamespace(mcp=SimpleNamespace(
            list_tools=AsyncMock(side_effect=RuntimeError("fixture unavailable")),
        ))
        monkeypatch.setattr(module, "importlib", SimpleNamespace(import_module=lambda name: fake))
    if consumer == "dashboard":
        with Flask(__name__).app_context():
            result = (await dashboard.mcp_tools.__wrapped__()).get_json()
    else:
        result = await snapshot.mcp_status()
    assert set(result) == {"health", "memory", "outreach", "recon"}
    catalog = json.loads((Path(__file__).with_name("fixtures") / "mcp_catalogs_v4.json").read_text())
    for server, status in result.items():
        if broken:
            expected = {"status": "error", "error": "fixture unavailable"}
            if consumer == "dashboard":
                expected["tools"] = []
        elif consumer == "dashboard":
            expected = {"status": "up", "tools": sorted(tool["name"] for tool in catalog[server])}
        else:
            expected = {"status": "up", "tool_count": len(catalog[server])}
        assert status == expected
