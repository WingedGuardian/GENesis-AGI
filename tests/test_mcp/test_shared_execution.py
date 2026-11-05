"""The shared execution seam preserves upstream state and owns handler retirement."""

import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[2]
QUALIFIED = {("2.14.6", "1.30.0"), ("2.14.7", "1.30.0")}


def configured(monkeypatch, pair=("2.14.6", "1.30.0")):
    from genesis.mcp import shared_execution

    monkeypatch.setattr(shared_execution, "version", lambda name: dict(zip(("fastmcp", "mcp"), pair, strict=True))[name])
    return shared_execution


@pytest.mark.parametrize("pair", [("2.14.6", "1.30.0"), ("2.14.7", "1.30.0")])
def test_original_server_and_initialization_contract_are_preserved(monkeypatch, pair):
    module = configured(monkeypatch, pair)
    # Keep FastMCP strongly referenced: its low-level server intentionally uses a weakref.
    fastmcp = FastMCP("fixture")
    server = fastmcp._mcp_server
    handler_map, original_run = server.request_handlers, server.run.__func__
    adapter = module.SharedExecution(server)
    marker = object()
    server.create_initialization_options = Mock(return_value=marker)
    assert adapter.create_initialization_options("fixture", experimental_capabilities={}) is marker
    server.create_initialization_options.assert_called_once_with("fixture", experimental_capabilities={})
    assert fastmcp._mcp_server is server and server.request_handlers is handler_map
    assert server.run.__func__ is original_run


@pytest.mark.parametrize("pair", [("2.14.8", "1.30.0"), ("3.0.0", "1.30.0"),
                                  ("2.14.6", "1.26.0"), ("2.14.6", "1.31.0")])
def test_unqualified_dependencies_refuse_before_execution(monkeypatch, pair):
    module = configured(monkeypatch, pair)
    fastmcp = FastMCP("fixture")
    with pytest.raises(module.SharedExecutionError, match="qualified"):
        module.SharedExecution(fastmcp._mcp_server)


def test_missing_package_metadata_is_a_compatibility_error(monkeypatch):
    module = configured(monkeypatch)
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(module, "version", missing)
    fastmcp = FastMCP("fixture")
    with pytest.raises(module.SharedExecutionError, match="qualified"):
        module.SharedExecution(fastmcp._mcp_server)


@pytest.mark.parametrize("kind", ["foreign", "subclass", "instance-override", "class-override"])
def test_unsupported_execution_objects_refuse(monkeypatch, kind):
    module = configured(monkeypatch)
    fastmcp = FastMCP("fixture")
    server = fastmcp._mcp_server
    if kind == "foreign":
        server = object()
    elif kind == "subclass":
        class CustomServer(module.LowLevelServer):
            pass
        server = CustomServer(fastmcp=fastmcp, name="fixture")
    elif kind == "instance-override":
        server.run = Mock()
    else:
        monkeypatch.setattr(module.LowLevelServer, "run", Mock())
    with pytest.raises(module.SharedExecutionError, match="unmodified"):
        module.SharedExecution(server)


@pytest.mark.parametrize("mode", ["json", "sse"])
@pytest.mark.parametrize("trigger", ["delete", "idle", "shutdown", "cancel"])
@pytest.mark.parametrize("peer", [False, True])
def test_http_session_retirement(mode, trigger, peer):
    actual = tuple(importlib.metadata.version(name) for name in ("fastmcp", "mcp"))
    if actual not in QUALIFIED:
        pytest.skip("exact supported pair runs in the shared-execution qualification CI job")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=os.pathsep.join(filter(None, (str(ROOT / "src"), os.environ.get("PYTHONPATH")))))
    command = [sys.executable, str(Path(__file__).with_name("_shared_execution_http_probe.py")), mode, trigger]
    if peer:
        command.append("peer")
    result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.splitlines()[-1]) == {
        "mode": mode, "trigger": trigger, "peer": peer, "passed": True,
    }
