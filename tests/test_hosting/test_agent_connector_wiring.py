"""The agent connector's WIRING, which its own unit tests cannot cover.

`tests/test_dashboard/test_agent_api.py` builds a bare Flask app and registers
the blueprint by hand, so it passes whether or not the real host ever registers
it. Deleting the registration block in `standalone.py` would leave that suite
green and the endpoint absent. These tests bind the registration itself, in the
same shape as `test_openclaw_completions.py`.
"""
from __future__ import annotations

from flask import Flask


def _register(app: Flask) -> None:
    from genesis.dashboard.routes.agent_api import agent_api_bp

    if "agent_api" not in app.blueprints:
        app.register_blueprint(agent_api_bp)


def test_blueprint_puts_both_routes_on_the_url_map():
    app = Flask(__name__)
    _register(app)
    rules = {r.rule for r in app.url_map.iter_rules()}
    assert "/v1/agent/ping" in rules
    assert "/v1/agent/chat/completions" in rules


def test_registration_is_idempotent():
    """The host guards on `"agent_api" not in app.blueprints`; a second call
    must not raise, or a re-registering host crashes at boot."""
    app = Flask(__name__)
    _register(app)
    _register(app)
    assert "agent_api" in app.blueprints


def test_host_registers_the_connector_and_the_neutral_loop_alias():
    """Reads the host source for both wiring facts. A source assertion is weak
    on its own, which is why the route-map tests above exist; this one catches
    the registration block being deleted wholesale."""
    from pathlib import Path

    import genesis.hosting.standalone as standalone

    src = Path(standalone.__file__).read_text(encoding="utf-8")
    assert "agent_api_bp" in src, "connector blueprint not registered by the host"
    assert 'config["GENESIS_CONVERSATION_LOOP"]' in src, (
        "vendor-neutral loop alias missing — the connector reads this key and "
        "would answer 503 forever without it"
    )


def test_the_connector_reads_the_key_the_host_actually_sets():
    """The defect this pins really happened: the connector was first written
    against an invented config key, which no host ever set."""
    from pathlib import Path

    import genesis.dashboard.routes.agent_api as agent_api
    import genesis.hosting.standalone as standalone

    connector_src = Path(agent_api.__file__).read_text(encoding="utf-8")
    host_src = Path(standalone.__file__).read_text(encoding="utf-8")
    for key in ("GENESIS_CONVERSATION_LOOP", "GENESIS_EVENT_LOOP"):
        assert f'config.get("{key}")' in connector_src, f"connector does not read {key}"
        assert f'config["{key}"]' in host_src, f"host does not set {key}"
