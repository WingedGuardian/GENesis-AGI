"""The provider toggle route passes the operator's intent to the registry.

The dashboard renders "enable" or "disable" from a polled breaker state, and
reading a breaker's state can move an expired OPEN to HALF_OPEN between the
poll and the click. Only the action the button showed settles what the click
meant, so the route forwards it as ``enable`` and the registry decides from it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.routes import health as health_route
from genesis.dashboard.routes import providers as providers_route
from genesis.routing.types import ProviderState

_JS = Path(__file__).parents[2] / "src/genesis/dashboard/webui/js/dashboard.js"
_SIGNATURE = "async toggleProvider(name) {"


def _call(**request_kwargs):
    breakers = MagicMock()
    breakers.toggle.return_value = ProviderState.CLOSED
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.circuit_breakers = breakers
    app = Flask(__name__)
    with (
        patch("genesis.runtime.GenesisRuntime") as GR,
        patch.object(health_route, "invalidate_snapshot_cache"),
        app.test_request_context(
            "/api/genesis/providers/qdrant/toggle",
            method="POST",
            **request_kwargs,
        ),
    ):
        GR.instance.return_value = rt
        result = providers_route.provider_toggle("qdrant")
    resp, status = result if isinstance(result, tuple) else (result, result.status_code)
    return breakers, resp, status


@pytest.mark.parametrize("enabled", [True, False])
def test_boolean_intent_reaches_the_registry(enabled):
    breakers, _, status = _call(json={"enabled": enabled})
    assert status == 200
    breakers.toggle.assert_called_once_with("qdrant", enable=enabled)


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"json": {}}, {"data": "not json"}, {"data": "null", "content_type": "application/json"}],
)
def test_absent_intent_is_none(kwargs):
    breakers, _, status = _call(**kwargs)
    assert status == 200
    breakers.toggle.assert_called_once_with("qdrant", enable=None)


# An explicit null is not an absent key: forwarded as enable=None it ran the
# state-dependent toggle, so a bad enable request could disable a provider.
@pytest.mark.parametrize(
    "body",
    [
        {"enabled": "yes"},
        {"enabled": 1},
        {"enabled": 0},
        {"enabled": ""},
        {"enabled": None},
        True,
        [True],
        [],
        0,
        "x",
    ],
)
def test_malformed_intent_is_refused_without_toggling(body):
    breakers, resp, status = _call(json=body)
    assert status == 400
    assert resp.get_json()["status"] == "error"
    breakers.toggle.assert_not_called()


def test_enable_click_reopens_nothing_after_the_hold_expired(tmp_path):
    """Real registry through the real route: the reported failure, end to end."""
    from genesis.routing.circuit_breaker import CircuitBreakerRegistry
    from genesis.routing.types import ProviderConfig

    t = [0.0]
    cfg = ProviderConfig(
        name="qdrant",
        provider_type="openai",
        model_id="m",
        is_free=False,
        rpm_limit=None,
        open_duration_s=120,
    )
    reg = CircuitBreakerRegistry(
        {"qdrant": cfg}, state_file=tmp_path / "s.json", clock=lambda: t[0]
    )
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.circuit_breakers = reg
    app = Flask(__name__)

    def post(body):
        with (
            patch("genesis.runtime.GenesisRuntime") as GR,
            patch.object(health_route, "invalidate_snapshot_cache"),
            app.test_request_context(
                "/api/genesis/providers/qdrant/toggle", method="POST", json=body
            ),
        ):
            GR.instance.return_value = rt
            return providers_route.provider_toggle("qdrant").get_json()

    assert post({"enabled": False})["state"] == "open"
    cb = reg.get("qdrant")
    t[0] = cb._effective_open_duration() + 1
    assert cb.state is ProviderState.HALF_OPEN  # the routing poll's read
    assert post({"enabled": True}) == {
        "status": "ok",
        "name": "qdrant",
        "state": "closed",
        "enabled": True,
    }
    assert cb._state is ProviderState.CLOSED and cb._failure_cause is None


_HARNESS = r"""
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const calls = [];
globalThis.fetchApi = async (url, request) => { calls.push([url, request]); return {ok: true}; };
globalThis.alert = (m) => { throw new Error("alert: " + m); };
const out = {};
(async () => {
  for (const [state, label] of [["open", "enable"], ["half_open", "disable"], ["closed", "disable"]]) {
    const store = {
      routingConfig: {cb_states: {p: state}},
      breakerIsOpen(n) { return this.routingConfig.cb_states[n] === "open"; },
      fetchProvidersDetail: async () => {}, fetchRoutingConfig: async () => {},
    };
    store.toggleProvider = new AsyncFunction("name", BODY).bind(store);
    calls.length = 0;
    await store.toggleProvider("p");
    const [url, request] = calls[0];
    out[label + ":" + state] = {url, method: request.method,
      contentType: (request.headers || {})["Content-Type"], body: JSON.parse(request.body)};
  }
  console.log(JSON.stringify(out));
})().catch(err => { console.error(err); process.exitCode = 1; });
"""


def test_dashboard_sends_the_action_its_button_showed():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    source = _JS.read_text()
    start = source.index(_SIGNATURE) + len(_SIGNATURE)
    body = source[start : source.index("\n        },", start)]
    script = f"const BODY = {json.dumps(body)};\n" + _HARNESS
    ran = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    result = json.loads(ran.stdout)
    for key, enabled in [
        ("enable:open", True),
        ("disable:half_open", False),
        ("disable:closed", False),
    ]:
        sent = result[key]
        assert sent["url"] == "/api/genesis/providers/p/toggle"
        assert sent["method"] == "POST"
        assert sent["contentType"] == "application/json"
        assert sent["body"] == {"enabled": enabled}, key
