"""Every ``/v1/*`` route refuses an anonymous caller — enumerated, not spot-checked.

``dashboard/auth.py`` exempts the whole ``/v1/*`` prefix from the dashboard
SESSION gate, because machine callers have no browser session. The comment beside
``check_bearer_token`` says it exists "so a new ``/v1/*`` surface cannot quietly
ship without one" — but nothing enforced that, and a convention several authors
must each remember is exactly what a fourth blueprint quietly breaks.

POLARITY IS ALLOWLIST in both directions. Routes are discovered from a live
``url_map``, and the BLUEPRINTS that map is built from are discovered by PARSING
the production registration site rather than restated in a tuple here.

The claim is bounded on purpose, because the unbounded version of it has now
been wrong twice. An earlier version listed three blueprints by hand and said a
fourth was "covered by construction"; it was not — a fourth would never have
been registered here at all. The version after that parsed production but
recognised only two call shapes, while a THIRD shape was already in the file it
parsed, so a blueprint arriving that way was still skipped in silence. What
holds now is narrower and checkable: the three registration shapes production
actually uses are resolved, and a call this parser cannot resolve FAILS the
suite instead of disappearing from it. A fourth shape is covered only once it
is added below — the assertion is what makes that visible rather than silent.

A route that needs to be public must be named in ``_PUBLIC`` with a reason,
which makes the exemption reviewable instead of invisible.

Origin: one of these three endpoints shipped with no auth at all while
authenticating nothing, on a port bound 0.0.0.0, invoking Claude Code.
"""

from __future__ import annotations

import pytest
from flask import Flask

# Deliberately empty: no /v1 route is public today. An addition here needs a
# stated reason, and reviewing that line is the point of the collection.
_PUBLIC: dict[str, str] = {}


def _production_blueprints() -> tuple[list, list, list]:
    """Every blueprint the production app factory registers, by PARSING it.

    ``hosting/standalone.py::_register_blueprints`` is the one place that
    decides what the served app contains. Reading it — rather than restating it
    here — is what makes a surface added later covered rather than merely
    claimed: a new ``/v1`` blueprint registered there appears in this list
    automatically, and its routes then have to satisfy the assertions below.

    Two registration shapes are recognised, because production uses both:
    ``app.register_blueprint(NAME)`` with NAME imported inside the function, and
    an adapter that registers its own (``OpenClawAdapter().register_blueprints``),
    which is how the OpenClaw completions endpoint arrives.

    A blueprint whose import fails is SKIPPED rather than fatal — an optional
    subsystem absent from a test environment must not break the contract for the
    ones that are present. The guard test below is what stops that skipping from
    hollowing the suite out.
    """
    import ast
    import importlib
    from pathlib import Path

    from genesis.hosting import standalone

    tree = ast.parse(Path(standalone.__file__).read_text(encoding="utf-8"))
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_register_blueprints"
    )

    # local alias -> (module, attribute), from the imports inside the function
    imported: dict[str, tuple[str, str]] = {}
    for node in ast.walk(fn):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported[alias.asname or alias.name] = (node.module, alias.name)

    blueprints, adapters, helpers, unresolved = [], [], [], []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue

        # app.register_blueprint(NAME)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "register_blueprint":
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Name) and arg.id in imported:
                blueprints.append(imported[arg.id])
            else:
                unresolved.append(ast.dump(node))
            continue

        # Adapter().register_blueprints(app)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "register_blueprints":
            owner = node.func.value
            if (
                isinstance(owner, ast.Call)
                and isinstance(owner.func, ast.Name)
                and owner.func.id in imported
            ):
                adapters.append(imported[owner.func.id])
            else:
                unresolved.append(ast.dump(node))
            continue

        # register_something(app) — a free function handed the app. This shape
        # is ALREADY in the file (register_terminal_ws), and missing it is how
        # the previous version of this parser failed open: a blueprint arriving
        # this way was never registered here, so every assertion below passed
        # while inspecting an app that did not contain it. Keyed on being handed
        # `app`, which is what distinguishes a registrar from a helper that
        # takes something else (init_outreach_api(db=...) is not one).
        if isinstance(node.func, ast.Name) and node.func.id in imported:
            takes_app = any(isinstance(a, ast.Name) and a.id == "app" for a in node.args)
            if takes_app:
                helpers.append(imported[node.func.id])

    # Fail LOUDLY on a registration this parser cannot resolve. Silently
    # skipping is the failure mode this whole module exists to prevent, and a
    # skipped registration is indistinguishable from an absent one.
    assert not unresolved, (
        "registration shapes this parser cannot resolve — the contract would "
        f"silently skip whatever they register: {unresolved}"
    )

    def _load(entries):
        out = []
        for module_name, attr in entries:
            try:
                out.append(getattr(importlib.import_module(module_name), attr))
            except Exception:  # optional subsystem, or unimportable in this env
                continue
        return out

    return _load(blueprints), adapters, _load(helpers)


@pytest.fixture()
def app_with_every_v1_blueprint(monkeypatch):
    """One app carrying every blueprint PRODUCTION registers that serves /v1."""
    monkeypatch.setenv("GENESIS_MCP_HTTP_TOKEN", "contract-test-token")

    import importlib

    blueprints, adapters, helpers = _production_blueprints()

    app = Flask(__name__)
    app.config["TESTING"] = True
    # Production sets this, and it selects a DIFFERENT Werkzeug input-stream
    # wrapper — a bare app gets the raw stream, so a body-bound test on a bare
    # app exercises code the server never runs.
    app.config["MAX_CONTENT_LENGTH"] = 500 * 1024 * 1024
    for bp in blueprints:
        try:
            app.register_blueprint(bp)
        except Exception:  # noqa: PERF203 - one bad blueprint must not hide the rest
            continue
    for module_name, attr in adapters:
        try:
            getattr(importlib.import_module(module_name), attr)().register_blueprints(app)
        except Exception:
            continue
    for fn in helpers:
        try:
            fn(app)
        except Exception:
            continue
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
    vacuously true, which is the failure mode of a discovery-based test.

    Doubly so now that the blueprint set is discovered as well as the routes —
    a parse that silently matched nothing, or an import that quietly failed,
    would produce a passing suite that inspected an empty app.
    """
    rules = _v1_rules(app_with_every_v1_blueprint)
    assert len(rules) >= 7, f"expected the known /v1 surface, found {rules}"


def test_the_three_known_v1_surfaces_are_all_discovered(app_with_every_v1_blueprint):
    """The discovery must find the surfaces we KNOW exist.

    Counting routes is not enough: one blueprint contributing many routes would
    satisfy a count while another was missing entirely. These three paths are
    the ones this contract was written for, so name them — if the parse stops
    resolving any of them, that is a defect in the discovery, not a licence to
    check fewer surfaces.
    """
    paths = {rule for rule, _ in _v1_rules(app_with_every_v1_blueprint)}
    for expected in (
        "/v1/chat/completions",
        "/v1/desk/chat/completions",
        "/v1/voice/tool_call",
    ):
        assert expected in paths, f"{expected} was not discovered from production; found {sorted(paths)}"


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
