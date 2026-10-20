"""Agent Zero mode installs the app-level dashboard gates.

In Agent Zero hosting the dashboard blueprints are mounted by the server-startup
extension (``az_plugins/genesis/extensions/server_startup/_00_genesis_bootstrap.py``),
not by ``AgentZeroAdapter``. A host that mounts them without
``apply_dashboard_gates`` serves Genesis API routes with no Host, network-scope
or mutation gate, so the extension itself must install them.

The extension runs for real against a fresh Flask app. Agent Zero's
``Extension`` base, the runtime singleton and the heartbeat thread are stubbed:
nothing boots.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from flask import Flask

from genesis.dashboard import host_gate
from genesis.dashboard.auth import check_api_mutation_auth, check_api_network_scope

BOOTSTRAP = (
    Path(__file__).resolve().parents[2]
    / "az_plugins/genesis/extensions/server_startup/_00_genesis_bootstrap.py"
)


class _FakeRuntime:
    db = None

    async def bootstrap(self, mode):
        return None


class _FakeHeartbeat:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


@pytest.fixture()
def run_extension(monkeypatch, tmp_path):
    """Run the real extension against a fresh Flask app; return the app."""
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: tmp_path / "token")
    monkeypatch.setattr("genesis.dashboard.auth._internal_token_cache", None)
    host_gate._reset_cache()

    helpers = types.ModuleType("python.helpers.extension")
    helpers.Extension = type("Extension", (), {"__init__": lambda self, *a, **k: None})
    for name, mod in (
        ("python", types.ModuleType("python")),
        ("python.helpers", types.ModuleType("python.helpers")),
        ("python.helpers.extension", helpers),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    app = Flask("az_host")
    app.secret_key = "test"
    monkeypatch.setitem(sys.modules, "run_ui", types.SimpleNamespace(webapp=app))
    monkeypatch.setattr(sys.modules["__main__"], "webapp", None, raising=False)
    monkeypatch.setattr("genesis.runtime.GenesisRuntime.instance", lambda: _FakeRuntime())
    monkeypatch.setattr("genesis.dashboard.heartbeat.DashboardHeartbeat", _FakeHeartbeat)
    monkeypatch.setattr("genesis.outreach.api.init_outreach_api", lambda **kw: None)

    def run():
        spec = importlib.util.spec_from_file_location("az_genesis_bootstrap_under_test", BOOTSTRAP)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        asyncio.run(mod.GenesisBootstrap().execute())
        return app

    return run


@pytest.fixture()
def webapp(run_extension):
    return run_extension()


def test_extension_installs_every_dashboard_gate(webapp):
    hooks = webapp.before_request_funcs[None]
    assert host_gate.check_host_header in hooks
    assert check_api_network_scope in hooks
    assert check_api_mutation_auth in hooks
    # Host gate first, as apply_dashboard_gates orders it.
    assert hooks.index(host_gate.check_host_header) < hooks.index(check_api_network_scope)


def test_foreign_host_is_refused_in_agent_zero_mode(webapp):
    client = webapp.test_client()
    refused = client.get("/api/genesis/auth/status", headers={"Host": "evil.example:5000"})
    assert refused.status_code == 403
    allowed = client.get("/api/genesis/auth/status", headers={"Host": "127.0.0.1:5000"})
    assert allowed.status_code == 200


def test_gate_failure_mounts_no_dashboard(run_extension, monkeypatch):
    # Fail closed: if the gates cannot be installed, the dashboard, terminal
    # and outreach routes are not mounted at all rather than served ungated.
    def boom(app):
        raise OSError("token directory is read-only")

    monkeypatch.setattr("genesis.dashboard.auth.apply_dashboard_gates", boom)
    app = run_extension()
    assert "genesis_dashboard" not in app.blueprints
    assert "outreach_api" not in app.blueprints
    assert not any(r.rule == "/ws/terminal" for r in app.url_map.iter_rules())
    assert app.test_client().get("/api/genesis/auth/status").status_code == 404
