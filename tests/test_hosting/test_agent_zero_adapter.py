"""Tests for AgentZeroAdapter — protocol compliance and blueprint registration."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.hosting.agent_zero.adapter import AgentZeroAdapter


@pytest.fixture()
def adapter():
    return AgentZeroAdapter()


# ── Protocol compliance ──────────────────────────────────────────────


def test_adapter_has_name(adapter):
    assert adapter.name == "Agent Zero"


@pytest.mark.asyncio
async def test_serve_is_noop(adapter):
    """serve() must return without error — AZ owns the event loop."""
    await adapter.serve()


@pytest.mark.asyncio
async def test_shutdown_is_noop(adapter):
    """shutdown() must return without error — AZ owns shutdown."""
    await adapter.shutdown()


def test_get_flask_app_returns_none_when_no_main(adapter):
    """Returns None when __main__ has no webapp attribute."""
    import sys
    main = sys.modules.get("__main__")
    if hasattr(main, "webapp"):
        # Temporarily remove it
        original = main.webapp
        del main.webapp
        result = adapter.get_flask_app()
        main.webapp = original
    else:
        result = adapter.get_flask_app()
    assert result is None


# ── register_blueprints ──────────────────────────────────────────────


def test_register_blueprints_registers_dashboard():
    """register_blueprints() registers genesis_dashboard on the app."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    mock_rt = MagicMock()
    mock_rt.db = None

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        adapter = AgentZeroAdapter()
        adapter.register_blueprints(app)

    assert "genesis_dashboard" in app.blueprints


def test_register_blueprints_idempotent():
    """Calling register_blueprints twice doesn't register genesis_dashboard twice."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    mock_rt = MagicMock()
    mock_rt.db = None

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        adapter = AgentZeroAdapter()
        adapter.register_blueprints(app)
        adapter.register_blueprints(app)  # Second call

    assert app.blueprints.get("genesis_dashboard") is not None


# ── register_overlay ────────────────────────────────────────────────


def test_register_overlay_registers_genesis_ui():
    """register_overlay() registers genesis_ui on the app."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    adapter = AgentZeroAdapter()
    adapter.register_overlay(app)

    assert "genesis_ui" in app.blueprints


def test_register_overlay_static_assets_serve():
    """Static assets must be accessible after register_overlay()."""
    app = Flask(__name__)
    app.config["TESTING"] = True

    adapter = AgentZeroAdapter()
    adapter.register_overlay(app)

    with app.test_client() as c:
        resp = c.get("/genesis-ui/genesis-overlay.css")
        assert resp.status_code == 200

        resp = c.get("/genesis-ui/genesis-overlay.js")
        assert resp.status_code == 200
