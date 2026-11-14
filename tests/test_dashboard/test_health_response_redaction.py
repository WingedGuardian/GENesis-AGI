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


def _runtime(snapshot: dict | None = None):
    """A runtime whose health service hands every caller the SAME dict, as the
    real one does; returns (runtime, that shared dict)."""
    shared = copy.deepcopy(_SNAPSHOT if snapshot is None else snapshot)
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
def standalone(monkeypatch, tmp_path, request):
    app = _gated_app(monkeypatch, tmp_path, blueprint)
    rt, shared = _runtime(getattr(request, "param", None))
    # The route replaces ``bridge`` with what the bridge wrote to status.json,
    # so a snapshot's bridge section is served from that file, as it is live.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    if "bridge" in shared:
        (tmp_path / ".genesis").mkdir(exist_ok=True)
        (tmp_path / ".genesis" / "status.json").write_text(json.dumps(shared["bridge"]))
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
def agent_zero(monkeypatch, tmp_path, request):
    app = _gated_app(monkeypatch, tmp_path, _az_blueprint())
    rt, shared = _runtime(getattr(request, "param", None))
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


# --------------------------------------------------------------------------
# No free text at all reaches an uncredentialed caller
# --------------------------------------------------------------------------
#
# Every section populated the way its producer fills it, including the error
# branches: a failed gather section, failed provider probes, discarded queue
# items, a crashed MCP server, probe details a producer merges in whole. Each
# free-text value is unique, so a leak names its own source.

_TS = "2026-01-01T00:00:00+00:00"


def _free(label: str) -> str:
    """A unique free-text value: spaces and an absolute path, as error text has."""
    return f"synthetic {label} text from /nonexistent/{label}"


_RICH = {
    "timestamp": _TS,
    "infrastructure": {
        "genesis.db": {
            "status": "healthy",
            "latency_ms": 3.5,
            "wal_status": "ok",
            "a_future_count": 7,
        },
        "qdrant": {"status": "error", "error": _free("qdrant-error")},
        "scheduler": {"status": "unknown", "error": _free("scheduler-error")},
        "ambient": {"status": "degraded", "verdict": "stale", "message": _free("ambient-message")},
        "pids": {"status": "healthy", "scope": "user", "current": 40, "path": _free("pids-path")},
        # Producers merge probe details in whole (infrastructure.py, guardian):
        # a key added there later is free text until shown otherwise.
        "guardian": {"status": "healthy", "a_future_detail": _free("guardian-detail")},
        "cc_slots": [{"slot": "slot-1", "status": "live", "started_at": _TS}],
    },
    "queues": {
        "pending_embeddings": 2,
        "embedding_last_error": _free("embedding-error"),
        "deferred_items": [
            {
                "type": "work-a",
                "site": "site_a",
                "reason": _free("deferred-reason"),
                "age_s": 5,
                "attempts": 0,
            }
        ],
        "discarded_items": [
            {
                "id": "item-1",
                "type": "work-a",
                "site": "site_a",
                "status": "discarded",
                "reason": _free("discard-reason"),
                "error": _free("discard-error"),
                "attempts": 5,
                "age_s": 60,
                "discarded_at": _TS,
            }
        ],
        "discarded": {
            "known": True,
            "total": 1,
            "sample": [{"id": "item-1", "error": _free("sample-error")}],
        },
    },
    # The shape health_data._or_error gives a section whose producer raised.
    "cost": {"status": "error", "error": _free("failed-section")},
    "provider_health": {
        "provider-a": {
            "configured": True,
            "reachable": False,
            "latency_ms": None,
            "error": _free("probe-error"),
            "checked_at": _TS,
        }
    },
    "mcp_servers": {
        "memory": {"status": "error", "error": _free("mcp-error")},
        "health": {"status": "crashed", "error": _free("mcp-crash")},
    },
    "eval_staleness": {"status": "error", "providers": [], "error": _free("eval-error")},
    "api_keys": {
        "providers": {
            "provider-a": {
                "status": "failed",
                "provider_type": "type-a",
                "error": _free("key-error"),
                "cb_state": "open",
                "cb_reason": "quota_exhausted",
                "chain_usage": ["site_a"],
                "validated_at": _TS,
            }
        },
        "alerts": [
            {
                "severity": "warning",
                "provider_type": "type-a",
                "reason": _free("alert-reason"),
                # Code-written (snapshots/api_keys.py), so it is kept.
                "message": "Type-A API key missing — 1 call site(s) affected",
                "affected_sites": ["site_a"],
            }
        ],
    },
    "deploy_health": {
        "status": "attention",
        "git": {"head": "0123abcd", "live": "0123abcd", "commits_behind_upstream": 0},
        "live": {"state": "unreadable", "reason": _free("live-reason"), "base": _free("live-base")},
        "missing_units": ["genesis-a.service"],
    },
    "surplus": {
        "status": "stalled",
        "queue_depth": 3,
        "stall_reason": _free("stall-reason"),
        "liveness_error": _free("liveness-error"),
    },
    "services": {
        "sentinel": {
            "current_state": "healthy",
            "last_trigger_source": "guardian_dialogue",
            "last_trigger_reason": _free("trigger-reason"),
            "last_dispatch_at": _TS,
        },
        "host_framework": {
            "name": "framework-a",
            "status": "running",
            "detected": True,
            "details": {"note": _free("framework-detail")},
        },
    },
    "cc_sessions": {
        "fallback": {
            "is_fallback": True,
            "original": "model-a",
            "fallback": "model-b",
            "reason": "rate_limited",
            "since": _TS,
        },
        "peer_availability": [
            {"peer": "peer-a", "available": False, "reason": "usage_limit", "reset_at": _TS}
        ],
    },
    "resilience": {"level": "not configured", "summary": "Providers down: provider-a"},
    "bridge": {
        "human_summary": _free("bridge-summary"),
        "resilience_state": {"cc": "normal", "cloud": "degraded"},
        "scheduler_heartbeats": {"awareness": _TS},
    },
    "memory_health": {
        "status": "healthy",
        "wings": [["learning", 3]],
        "classes": [["fact", 5]],
        "distribution": {"top_tags": [[_free("memory-tag"), 12]]},
    },
    "reflex": {
        "top_classes": [{"class_key": "TimeoutErrorxobservability", "signals": 2}],
        # A map keyed by data rather than by a name: the key itself is text.
        "by_message": {_free("keyed-by-text"): 3},
    },
    "call_sites": {
        "site_a": {
            "status": "healthy",
            "description": "Code-authored description of site a.",
            "frequency": "Daily",
            "cost_policy": "Free (provider-a)",
            "last_run_provider": "provider-a",
            "last_run_model": "vendor/model-a",
            "last_run_at": _TS,
            "last_response": _free("model-output"),
            "chain_health": [
                {
                    "provider": "provider-a",
                    "model": "vendor/model-a",
                    "state": "closed",
                    "type": "type-a",
                }
            ],
        }
    },
}


def _free_values(value) -> list[str]:
    """Every free-text value planted in ``value``."""
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in _free_values(k) + _free_values(v)]
    if isinstance(value, list):
        return [s for v in value for s in _free_values(v)]
    return [value] if isinstance(value, str) and value.startswith("synthetic ") else []


#: (path, key): each place a free-text value was withheld, marked ``<key>_hidden``.
_MARKED = [
    (("infrastructure", "qdrant"), "error"),
    (("infrastructure", "scheduler"), "error"),
    (("infrastructure", "ambient"), "message"),
    (("infrastructure", "pids"), "path"),
    (("infrastructure", "guardian"), "a_future_detail"),
    (("queues",), "embedding_last_error"),
    (("queues", "deferred_items", 0), "reason"),
    (("queues", "discarded_items", 0), "reason"),
    (("queues", "discarded_items", 0), "error"),
    (("queues", "discarded", "sample", 0), "error"),
    (("cost",), "error"),
    (("provider_health", "provider-a"), "error"),
    (("mcp_servers", "memory"), "error"),
    (("mcp_servers", "health"), "error"),
    (("eval_staleness",), "error"),
    (("api_keys", "providers", "provider-a"), "error"),
    (("api_keys", "alerts", 0), "reason"),
    (("deploy_health", "live"), "reason"),
    (("deploy_health", "live"), "base"),
    (("surplus",), "stall_reason"),
    (("surplus",), "liveness_error"),
    (("services", "sentinel"), "last_trigger_reason"),
    (("services", "host_framework", "details"), "note"),
    (("bridge",), "human_summary"),
]

#: Paths an uncredentialed caller keeps, with the value the producer gave.
_KEPT = [
    ("timestamp",),
    ("infrastructure", "genesis.db", "status"),
    ("infrastructure", "genesis.db", "latency_ms"),
    ("infrastructure", "genesis.db", "wal_status"),
    ("infrastructure", "genesis.db", "a_future_count"),
    ("infrastructure", "qdrant", "status"),
    ("infrastructure", "scheduler", "status"),
    ("infrastructure", "ambient", "verdict"),
    ("infrastructure", "pids", "scope"),
    ("infrastructure", "pids", "current"),
    ("infrastructure", "cc_slots"),
    ("queues", "pending_embeddings"),
    ("queues", "discarded", "known"),
    ("queues", "discarded", "total"),
    ("queues", "discarded_items", 0, "id"),
    ("queues", "discarded_items", 0, "type"),
    ("queues", "discarded_items", 0, "site"),
    ("queues", "discarded_items", 0, "status"),
    ("queues", "discarded_items", 0, "attempts"),
    ("queues", "discarded_items", 0, "age_s"),
    ("queues", "discarded_items", 0, "discarded_at"),
    ("queues", "deferred_items", 0, "attempts"),
    ("cost", "status"),
    ("provider_health", "provider-a", "configured"),
    ("provider_health", "provider-a", "reachable"),
    ("provider_health", "provider-a", "latency_ms"),
    ("provider_health", "provider-a", "checked_at"),
    ("mcp_servers", "memory", "status"),
    ("mcp_servers", "health", "status"),
    ("eval_staleness", "status"),
    ("eval_staleness", "providers"),
    ("api_keys", "providers", "provider-a", "status"),
    ("api_keys", "providers", "provider-a", "provider_type"),
    ("api_keys", "providers", "provider-a", "cb_state"),
    ("api_keys", "providers", "provider-a", "cb_reason"),
    ("api_keys", "alerts", 0, "message"),
    ("api_keys", "providers", "provider-a", "chain_usage"),
    ("api_keys", "providers", "provider-a", "validated_at"),
    ("api_keys", "alerts", 0, "severity"),
    ("api_keys", "alerts", 0, "provider_type"),
    ("api_keys", "alerts", 0, "affected_sites"),
    ("deploy_health", "status"),
    ("deploy_health", "git"),
    ("deploy_health", "live", "state"),
    ("deploy_health", "missing_units"),
    ("surplus", "status"),
    ("surplus", "queue_depth"),
    ("services", "sentinel", "current_state"),
    ("services", "sentinel", "last_trigger_source"),
    ("services", "sentinel", "last_dispatch_at"),
    ("services", "host_framework", "name"),
    ("services", "host_framework", "status"),
    ("services", "host_framework", "detected"),
    ("cc_sessions",),
    ("resilience",),
    ("bridge", "resilience_state"),
    ("bridge", "scheduler_heartbeats"),
    ("memory_health", "status"),
    ("memory_health", "wings"),
    ("memory_health", "classes"),
    ("reflex", "top_classes"),
    ("call_sites", "site_a", "status"),
    ("call_sites", "site_a", "description"),
    ("call_sites", "site_a", "frequency"),
    ("call_sites", "site_a", "cost_policy"),
    ("call_sites", "site_a", "last_run_provider"),
    ("call_sites", "site_a", "last_run_model"),
    ("call_sites", "site_a", "last_run_at"),
    ("call_sites", "site_a", "chain_health"),
]


def _at(value, path):
    for step in path:
        value = value[step]
    return value


def _assert_no_free_text(body: dict) -> None:
    leaked = [v for v in _free_values(_RICH) if v in json.dumps(body)]
    assert not leaked, f"free text reached an uncredentialed caller: {leaked}"
    for path, key in _MARKED:
        node = _at(body, path)
        assert key not in node, (path, key, node)
        assert node.get(f"{key}_hidden") is True, f"{path}: {key} withheld without a marker"
    for path in _KEPT:
        assert _at(body, path) == _at(_RICH, path), f"{path} was withheld or changed"
    # A list keeps its shape: the withheld tag becomes null beside its count.
    assert body["memory_health"]["distribution"] == {
        "top_tags": [[None, 12]],
        "top_tags_hidden": True,
    }
    assert body["reflex"]["by_message"] == {"keys_hidden": True}
    assert body["free_text_hidden"] is True


@pytest.mark.parametrize("standalone", [_RICH], indirect=True)
def test_no_free_text_reaches_an_uncredentialed_caller(standalone):
    app, shared = standalone
    _assert_no_free_text(_health(app.test_client()))
    assert shared == _RICH, "the shared snapshot was modified by a redacted response"


def test_the_status_code_is_decided_before_redaction(standalone, monkeypatch):
    """The supervisor and the deploy scripts act on the status code an
    uncredentialed caller gets, so what the redaction withholds must never
    change it: a redaction that removed the database section entirely still
    answers 200 for a healthy database."""
    import genesis.dashboard.routes.health as health_mod

    real = health_mod.redact_for_untrusted

    def _strip_everything(snapshot):
        real(snapshot)
        snapshot.pop("infrastructure", None)

    monkeypatch.setattr(health_mod, "redact_for_untrusted", _strip_everything)
    app, _ = standalone
    body = _health(app.test_client())
    assert body["status"] == "healthy"
    assert "infrastructure" not in body


@pytest.mark.parametrize("standalone", [_RICH], indirect=True)
def test_a_password_without_a_session_gets_no_free_text(standalone, monkeypatch):
    app, shared = standalone
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    _assert_no_free_text(_health(app.test_client()))
    assert shared == _RICH


@pytest.mark.parametrize("standalone", [_RICH], indirect=True)
def test_credentialed_callers_get_every_field_unchanged(standalone, monkeypatch):
    """The mirror, for both credentials: nothing withheld, nothing marked."""
    app, shared = standalone
    token = auth_mod.get_or_create_internal_api_token()
    body = _health(app.test_client(), Authorization=f"Bearer {token}")
    assert {k: v for k, v in body.items() if k != "status"} == _RICH

    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    client = app.test_client()
    assert client.post("/api/genesis/auth/login", json={"password": "pw"}).status_code == 200
    body = _health(client)
    assert {k: v for k, v in body.items() if k != "status"} == _RICH
    assert shared == _RICH


@pytest.mark.parametrize("agent_zero", [_RICH], indirect=True)
async def test_the_agent_zero_route_withholds_free_text_too(agent_zero):
    app, shared = agent_zero
    _assert_no_free_text(_health(app.test_client()))
    assert shared == _RICH
    token = auth_mod.get_or_create_internal_api_token()
    assert _health(app.test_client(), Authorization=f"Bearer {token}") == _RICH


def test_a_safe_key_carrying_free_text_is_withheld():
    """A status, name or id is kept only while it LOOKS like one: an enum key
    a producer one day fills with an exception's text fails closed."""
    from genesis.dashboard.routes.health import _withhold_free_text

    source = {
        "probe": {"status": _free("status-text"), "name": "/nonexistent/abs-path", "id": "item-1"},
        "names": ["ok-name", _free("list-item")],
    }
    before = copy.deepcopy(source)
    reduced = _withhold_free_text(source)
    assert source == before, "the walk modified its input"
    assert reduced == {
        "probe": {"status_hidden": True, "name_hidden": True, "id": "item-1"},
        "names": [None, None],
        "names_hidden": True,
    }, reduced


def test_tuples_and_objects_cannot_carry_text_past_the_walk():
    """Producers return tuples (memory_health's ``(name, count)`` rows), which
    the JSON encoder writes as arrays, and the encoder expands a dataclass into
    its fields. Neither may skip the walk."""
    from dataclasses import dataclass

    from genesis.dashboard.routes.health import _withhold_free_text

    @dataclass
    class Detail:
        note: str

    reduced = _withhold_free_text(
        {
            "top_tags": [(_free("tuple-tag"), 4)],
            "wings": [("learning", 3)],
            "detail": Detail(_free("dataclass-note")),
            "items": [Detail(_free("listed-dataclass"))],
        }
    )
    assert reduced == {
        "top_tags": [[None, 4]],
        "top_tags_hidden": True,
        "wings": [["learning", 3]],
        "detail_hidden": True,
        "items": [None],
        "items_hidden": True,
    }, reduced


def test_the_walk_rebuilds_every_container():
    """No nested dict or list of the input is reused in the output, so a later
    change to the response can never reach the cached snapshot."""
    from genesis.dashboard.routes.health import _withhold_free_text

    reduced = _withhold_free_text(_RICH)

    def containers(value):
        if isinstance(value, dict):
            yield value
            for v in value.values():
                yield from containers(v)
        elif isinstance(value, list):
            yield value
            for v in value:
                yield from containers(v)

    source_ids = {id(c) for c in containers(_RICH)}
    assert not [c for c in containers(reduced) if id(c) in source_ids]


# --------------------------------------------------------------------------
# The pages say the text was withheld
# --------------------------------------------------------------------------

_HELPERS_START = "// -- withheld text helpers --"
_HELPERS_END = "// -- end withheld text helpers --"


@pytest.mark.parametrize("page", sorted(_MONITOR_PAGES))
def test_the_monitor_page_marks_withheld_error_text(page):
    """Each monitor page reads the markers and says what was withheld, setting
    text only (never markup)."""
    source = (_REPO_ROOT / _MONITOR_PAGES[page]).read_text()
    helpers = source[source.index(_HELPERS_START) : source.index(_HELPERS_END)]
    assert "innerHTML" not in helpers
    assert 'id="text-hidden-note"' in source, f"{page}: no page-level note element"
    assert "showTextHiddenNote(lastData)" in source, f"{page}: the note is never updated"
    assert "hiddenText(item, 'error')" in source and "hiddenText(item, 'reason')" in source, (
        f"{page}: the discarded-item card does not read the markers"
    )

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available; the static checks above still apply")
    script = (
        "const note={style:{},textContent:''};\n"
        "const document={getElementById:id=>id==='text-hidden-note'?note:null};\n"
        + helpers
        + "\nconst out={};\n"
        "out.shown=hiddenText({error:'boom'},'error');\n"
        "out.hidden=hiddenText({error_hidden:true},'error');\n"
        "out.none=hiddenText({},'error');\n"
        "showTextHiddenNote({free_text_hidden:true});\n"
        "out.note=note.textContent; out.noteShown=note.style.display;\n"
        "showTextHiddenNote({});\n"
        "out.noteCredentialed=note.style.display;\n"
        "process.stdout.write(JSON.stringify(out));\n"
    )
    out = json.loads(
        subprocess.run(
            [node, "-e", script], capture_output=True, text=True, check=True, timeout=60
        ).stdout
    )
    assert out["shown"] == "boom"
    assert "hidden" in out["hidden"].lower() and "sign in" in out["hidden"].lower(), out
    assert out["none"] == ""
    assert "dashboard password" in out["note"] and out["noteShown"] == "block", out
    assert out["noteCredentialed"] == "none", out


def test_the_dashboard_header_says_detail_text_is_hidden():
    header = (
        _REPO_ROOT / "src/genesis/dashboard/templates/partials/chrome/header.html"
    ).read_text()
    assert "health?.free_text_hidden" in header, "the dashboard never reads the marker"
    assert "DASHBOARD_PASSWORD" in header


def test_queue_counter_errors_reach_an_uncredentialed_caller():
    # snapshots/queues.py writes only fixed strings here, and the dashboard's
    # queue verdict reads their "<counter>:" prefix (dashboard.js queuesSemantic).
    from genesis.dashboard.routes.health import _withhold_free_text

    queues = {"errors": ["discarded: count query failed"], "discarded": {"known": True}}
    reduced = _withhold_free_text({"queues": queues})
    assert reduced["queues"]["errors"] == ["discarded: count query failed"]
    assert "errors_hidden" not in reduced["queues"]


def test_code_written_provider_alert_text_reaches_an_uncredentialed_caller():
    # snapshots/api_keys.py builds the alert message and breaker label from
    # provider type names, counts and call-site ids; the header banner renders
    # the message, the provider panel the label.
    from genesis.dashboard.routes.health import _withhold_free_text

    message = "Type-A API key missing — 2 call site(s) affected"
    source = {
        "api_keys": {
            "alerts": [{"severity": "critical", "message": message}],
            "providers": {"provider-a": {"cb_state": "open", "cb_label": "Out of credits"}},
        }
    }
    reduced = _withhold_free_text(source)
    assert reduced["api_keys"]["alerts"][0]["message"] == message
    assert reduced["api_keys"]["providers"]["provider-a"]["cb_label"] == "Out of credits"


def test_an_email_shaped_key_is_text_and_drops_its_entry():
    from genesis.dashboard.routes.health import _withhold_free_text

    reduced = _withhold_free_text({"by_message": {"user@example.invalid": 3, "ok_key": 1}})
    assert reduced["by_message"] == {"keys_hidden": True, "ok_key": 1}
