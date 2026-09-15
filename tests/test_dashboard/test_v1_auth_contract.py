"""Every ``/v1/*`` route refuses an anonymous caller — enumerated, not spot-checked.

``dashboard/auth.py`` exempts the whole ``/v1/*`` prefix from the dashboard
SESSION gate, because machine callers have no browser session. The comment beside
``check_bearer_token`` says it exists "so a new ``/v1/*`` surface cannot quietly
ship without one" — but nothing enforced that, and a convention several authors
must each remember is exactly what a fourth blueprint quietly breaks.

POLARITY IS ALLOWLIST: this discovers routes from the live ``url_map`` rather
than checking a hand-written list, so a ``/v1`` surface added next year is
covered by construction. A route that needs to be public must be named in
``_PUBLIC`` with a reason, which makes the exemption reviewable instead of
invisible.

Origin: one of these three endpoints shipped with no auth at all while
authenticating nothing, on a port bound 0.0.0.0, invoking Claude Code.
"""

from __future__ import annotations

import pytest
from flask import Flask

# Deliberately empty: no /v1 route is public today. An addition here needs a
# stated reason, and reviewing that line is the point of the collection.
_PUBLIC: dict[str, str] = {}


@pytest.fixture()
def app_with_every_v1_blueprint(monkeypatch):
    """One app carrying every blueprint that serves a /v1 route."""
    monkeypatch.setenv("GENESIS_MCP_HTTP_TOKEN", "contract-test-token")

    from genesis.dashboard.routes.desk_api import desk_api_bp
    from genesis.dashboard.routes.voice_api import voice_api_bp
    from genesis.hosting.openclaw.completions import blueprint as openclaw_bp

    app = Flask(__name__)
    app.config["TESTING"] = True
    for bp in (openclaw_bp, voice_api_bp, desk_api_bp):
        app.register_blueprint(bp)
    return app


def _v1_rules(app):
    out = []
    for rule in app.url_map.iter_rules():
        if not rule.rule.startswith("/v1/"):
            continue
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            out.append((rule.rule, method))
    return sorted(out)


def test_the_enumeration_finds_routes_at_all(app_with_every_v1_blueprint):
    """Guard-the-guard: an empty enumeration would make every assertion below
    vacuously true, which is the failure mode of a discovery-based test."""
    rules = _v1_rules(app_with_every_v1_blueprint)
    assert len(rules) >= 7, f"expected the known /v1 surface, found {rules}"


def test_every_v1_route_refuses_an_anonymous_caller(app_with_every_v1_blueprint):
    """Each must answer 401 naming the Authorization header — never 2xx.

    A 404/405 would mean the route does not exist as enumerated, which is a
    defect in this test rather than a pass.
    """
    client = app_with_every_v1_blueprint.test_client()
    offenders = []
    for path, method in _v1_rules(app_with_every_v1_blueprint):
        if path in _PUBLIC:
            continue
        resp = client.open(path, method=method, json={})
        body = resp.get_data(as_text=True)
        # The STATUS alone is not enough, and this is the trap that made an
        # earlier version of this gate blind: an un-bootstrapped runtime also
        # answers 503, so accepting 503 let a route with its auth check DELETED
        # pass. The refusal must be an AUTH refusal — 401 naming the header.
        if resp.status_code != 401 or "Authorization header" not in body:
            offenders.append(f"{method} {path} -> {resp.status_code} {body[:80]!r}")
    assert not offenders, (
        "these /v1 routes did not refuse an UNAUTHENTICATED request on AUTH "
        "grounds: " + "; ".join(offenders)
    )


def test_a_valid_token_is_not_refused_by_the_auth_layer(app_with_every_v1_blueprint):
    """The negative control. Without it, a gate that refused EVERYTHING would
    pass the test above and look healthy."""
    client = app_with_every_v1_blueprint.test_client()
    client.environ_base["HTTP_AUTHORIZATION"] = "Bearer contract-test-token"
    still_refusing = []
    for path, method in _v1_rules(app_with_every_v1_blueprint):
        resp = client.open(path, method=method, json={})
        # Past auth, a route may well answer 400/503/500 — it has no runtime
        # behind it here. What it must NOT do is answer 401.
        if resp.status_code == 401:
            still_refusing.append(f"{method} {path}")
    assert not still_refusing, (
        "these routes refused a VALID token, so the check above proves nothing: "
        + "; ".join(still_refusing)
    )
