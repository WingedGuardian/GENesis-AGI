"""`GET /api/genesis/health` end to end: who is served a call site's model output.

tests/test_dashboard/test_health_cache.py pins the route function with the
trust predicate patched. This file drives the three surfaces that change
together, with nothing patched but the runtime's data:

* the standalone route through a Flask client, the app-level gates installed
  and a real login, in every password/session combination;
* the Agent Zero adapter's own copy of the route, loaded the way its bootstrap
  loads it, which must apply the same redaction;
* both monitor pages, whose detail panel has to say the text was withheld
  rather than show nothing.

Addresses are RFC 5737 documentation addresses; every value is synthetic.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

# Imported for its routes: the health route registers on the shared blueprint.
import genesis.dashboard.routes.health  # noqa: F401
from genesis.dashboard import auth as auth_mod
from genesis.dashboard import host_gate
from genesis.dashboard._blueprint import blueprint
from genesis.observability.health_data import HealthDataService

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OUTSIDE = "198.51.100.7"  # an untrusted network: the route answers it anyway
_TEXT = "synthetic model output"
_CHECKOUT_FILE = "edited-file.txt"

_SNAPSHOT = {
    "infrastructure": {"genesis.db": {"status": "healthy"}},
    "deploy_health": {
        "status": "attention",
        "main_checkout": {"status": "dirty", "count": 1, "paths": [_CHECKOUT_FILE]},
    },
    "call_sites": {
        "site_with_output": {
            "status": "healthy",
            "last_run_at": "2026-01-01T00:00:00+00:00",
            "last_run_provider": "provider-a",
            "last_response": _TEXT,
        },
        "site_without_output": {"status": "idle", "last_response": None},
    },
}


def _runtime():
    """A runtime whose health service hands every caller the SAME dict, as the
    real one does; returns (runtime, that shared dict)."""
    shared = copy.deepcopy(_SNAPSHOT)
    svc = HealthDataService()

    async def _compute():
        return shared

    svc._compute_snapshot = _compute
    rt = MagicMock()
    rt.is_bootstrapped = True
    rt.health_data = svc
    return rt, shared


def _gated_app(monkeypatch, tmp_path, bp) -> Flask:
    """An app with the production gates installed and ``bp`` mounted."""
    monkeypatch.setattr("genesis.env.internal_api_token_path", lambda: tmp_path / "token")
    monkeypatch.setattr(auth_mod, "_internal_token_cache", None)
    for name in (
        "DASHBOARD_PASSWORD",
        "GENESIS_DASHBOARD_TRUSTED_NETWORKS",
        "GENESIS_DASHBOARD_NETWORK_SCOPE",
        "GENESIS_DASHBOARD_HOSTNAME_CHECK",
        "GENESIS_DASHBOARD_ALLOWED_HOSTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(host_gate, "_run_tailscale_status", lambda: None)
    host_gate._reset_cache()
    app = Flask(__name__)
    app.secret_key = "test-secret-key"
    auth_mod.apply_dashboard_gates(app)
    app.register_blueprint(bp)
    return app


def _health(client, **headers):
    resp = client.get(
        "/api/genesis/health", environ_base={"REMOTE_ADDR": _OUTSIDE}, headers=headers
    )
    assert resp.status_code == 200, resp.get_data(as_text=True)
    return resp.get_json()


def _assert_withheld(body: dict) -> None:
    """The response text is gone and marked; the rest of each site survives."""
    site = body["call_sites"]["site_with_output"]
    assert _TEXT not in json.dumps(body), "the model's output reached an uncredentialed caller"
    assert "last_response" not in site
    assert site["last_response_hidden"] is True
    assert site["status"] == "healthy" and site["last_run_provider"] == "provider-a"
    assert body["call_sites"]["site_without_output"] == {"status": "idle"}


# --------------------------------------------------------------------------
# The standalone route, through the real request path
# --------------------------------------------------------------------------


@pytest.fixture()
def standalone(monkeypatch, tmp_path):
    app = _gated_app(monkeypatch, tmp_path, blueprint)
    rt, shared = _runtime()
    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        yield app, shared
    host_gate._reset_cache()


def test_no_password_means_nobody_is_shown_the_response_text(standalone):
    """With no dashboard password there is no credential to present, so the
    text is withheld from every browser, a posted session flag included."""
    app, shared = standalone
    client = app.test_client()
    _assert_withheld(_health(client))

    assert client.post("/api/genesis/auth/login", json={"password": "anything"}).status_code == 200
    with client.session_transaction() as sess:
        sess["authenticated"] = True
    _assert_withheld(_health(client))
    assert shared == _SNAPSHOT, "the shared snapshot was modified by a redacted response"


def test_a_password_without_a_session_still_withholds(standalone, monkeypatch):
    app, _ = standalone
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    _assert_withheld(_health(app.test_client()))


def test_a_signed_in_session_is_shown_the_response_text(standalone, monkeypatch):
    """The mirror: the redaction must not become 'withhold always'."""
    app, shared = standalone
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    client = app.test_client()
    assert client.post("/api/genesis/auth/login", json={"password": "pw"}).status_code == 200

    body = _health(client)
    assert body["call_sites"] == _SNAPSHOT["call_sites"]
    assert body["deploy_health"]["main_checkout"]["paths"] == [_CHECKOUT_FILE]

    # A second caller with no cookie, served from the same cached snapshot.
    _assert_withheld(_health(app.test_client()))
    assert shared == _SNAPSHOT


def test_the_internal_bearer_is_shown_the_response_text(standalone):
    """The token a local process reads from its 0600 file, with no password set."""
    app, _ = standalone
    token = auth_mod.get_or_create_internal_api_token()
    body = _health(app.test_client(), Authorization=f"Bearer {token}")
    assert body["call_sites"] == _SNAPSHOT["call_sites"]
    _assert_withheld(_health(app.test_client(), Authorization="Bearer not-the-token"))


# --------------------------------------------------------------------------
# The Agent Zero adapter's copy of the route
# --------------------------------------------------------------------------


def _az_blueprint():
    """Load the adapter module from its file, as its bootstrap does."""
    spec = importlib.util.spec_from_file_location(
        "genesis_api_health_under_test", _REPO_ROOT / "az_plugins" / "genesis" / "api_health.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.blueprint


@pytest.fixture()
def agent_zero(monkeypatch, tmp_path):
    app = _gated_app(monkeypatch, tmp_path, _az_blueprint())
    rt, shared = _runtime()
    with patch("genesis.runtime.GenesisRuntime") as GR:
        GR.instance.return_value = rt
        yield app, shared
    host_gate._reset_cache()


async def test_the_agent_zero_route_withholds_the_same_fields(agent_zero):
    """It is a second implementation of the same URL, so it owes the same
    answer: no response text and no checkout file names without a credential.
    Async so the route takes its running-loop branch deterministically."""
    app, shared = agent_zero
    body = _health(app.test_client())
    _assert_withheld(body)
    assert _CHECKOUT_FILE not in json.dumps(body), body["deploy_health"]
    assert body["deploy_health"]["main_checkout"] == {"status": "dirty", "count": 1}
    assert shared == _SNAPSHOT, "the adapter route modified the snapshot other callers share"


async def test_the_agent_zero_route_serves_a_credentialed_caller_in_full(agent_zero):
    app, _ = agent_zero
    token = auth_mod.get_or_create_internal_api_token()
    body = _health(app.test_client(), Authorization=f"Bearer {token}")
    assert body["call_sites"] == _SNAPSHOT["call_sites"]
    assert body["deploy_health"] == _SNAPSHOT["deploy_health"]


async def test_the_agent_zero_route_never_answers_unredacted_when_redaction_fails(
    agent_zero, monkeypatch
):
    """If the shared redaction cannot run, the route's answer is its generic
    error, never the snapshot as computed."""
    app, _ = agent_zero

    def _broken(_snapshot):
        raise RuntimeError("redaction unavailable")

    monkeypatch.setattr(genesis.dashboard.routes.health, "redact_for_untrusted", _broken)
    resp = app.test_client().get("/api/genesis/health", environ_base={"REMOTE_ADDR": _OUTSIDE})
    assert resp.status_code == 500
    assert _TEXT not in resp.get_data(as_text=True)
    assert _CHECKOUT_FILE not in resp.get_data(as_text=True)


# --------------------------------------------------------------------------
# Both monitor pages say the text was withheld
# --------------------------------------------------------------------------

_MONITOR_PAGES = {
    "standalone": "src/genesis/dashboard/templates/neural_monitor.html",
    "agent_zero": "az_plugins/genesis/templates/neural_monitor.html",
}
_BLOCK_START = "// Full response in scrollable box"
_BLOCK_END = "} else {\n        content.appendChild(sectionRow('Last Run', 'No runs recorded'));"


def _response_block(page: str) -> tuple[str, str]:
    """(DOM helpers, the Last Response block) cut out of a monitor page."""
    source = (_REPO_ROOT / _MONITOR_PAGES[page]).read_text()
    el_fn = source[source.index("function el(") : source.index("function formatAge(")]
    start = source.index(_BLOCK_START)
    return el_fn, source[start : source.index(_BLOCK_END, start)]


@pytest.mark.parametrize("page", sorted(_MONITOR_PAGES))
def test_the_monitor_page_explains_a_withheld_response(page):
    el_fn, block = _response_block(page)
    assert "last_response_hidden" in block, f"{page}: the page never reads the withheld marker"
    assert "innerHTML" not in block, f"{page}: the response box must be filled as text"

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; the static checks above still apply")
    script = (
        "const item={data:JSON.parse(require('fs').readFileSync(0,'utf8'))};\n"
        "const document={createElement:tag=>({tag,style:{},textContent:'',children:[],\n"
        "    appendChild(child){this.children.push(child);}})};\n"
        "const content=document.createElement('div');\n"
        + el_fn
        + block
        + "\nfunction text(e){return [e.textContent].concat(e.children.map(text)).join('|');}\n"
        "process.stdout.write(text(content));\n"
    )

    def render(data: dict) -> str:
        return subprocess.run(
            [node, "-e", script],
            input=json.dumps(data),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout

    shown = render({"last_response": _TEXT})
    assert "Last Response" in shown and _TEXT in shown
    assert "password" not in shown, f"{page}: the hint rendered beside a visible response"

    withheld = render({"last_response_hidden": True})
    assert "Last Response" in withheld, f"{page}: a withheld response renders as nothing"
    assert "dashboard password" in withheld and "sign in" in withheld.lower(), withheld

    assert "Last Response" not in render({}), f"{page}: a site with no output grew a box"
