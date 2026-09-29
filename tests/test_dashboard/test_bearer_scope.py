"""The desk endpoint's credential is scoped to the desk endpoint (#2442), and every
reader of these bearer tokens agrees on what "configured" means (#2110).

Why this exists: until this change ONE token authenticated every ``/v1`` route —
tool execution, memory writes, and a route that starts a Claude Code subprocess —
so a desktop client configured to ask Genesis to think necessarily held a
credential for all of it. ``GENESIS_DESK_TOKEN`` is accepted by the desk route
only. The broad token keeps working there during a transition, so an install
that already points a client at it does not break on update.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from flask import Flask

from genesis.dashboard.auth import check_bearer_token
from genesis.env import bearer_token
from tests.conftest import private_module

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_SCRIPT = _REPO_ROOT / "scripts" / "genesis_mcp_server.py"

_BROAD = "GENESIS_MCP_HTTP_TOKEN"
_DESK = "GENESIS_DESK_TOKEN"
_DESK_ACCEPT = (_DESK, _BROAD)


@pytest.fixture()
def app():
    return Flask(__name__)


@pytest.fixture(autouse=True)
def _no_ambient_tokens(monkeypatch):
    # A developer's shell or a sibling test may carry either token; every test
    # here states exactly which ones exist.
    monkeypatch.delenv(_BROAD, raising=False)
    monkeypatch.delenv(_DESK, raising=False)


def _check(app, header: str | None, **kwargs):
    headers = {"Authorization": header} if header is not None else {}
    with app.test_request_context(headers=headers):
        return check_bearer_token("test surface", **kwargs)


# ── the scope: the whole point of the change ──────────────────────────────────


def test_the_desk_token_opens_the_desk_route(app, monkeypatch):
    monkeypatch.setenv(_DESK, "desk-only")
    assert _check(app, "Bearer desk-only", accept=_DESK_ACCEPT) is None


def test_the_desk_token_does_not_open_a_default_scoped_route(app, monkeypatch):
    """The narrowing itself. With both tokens configured, a route on the default
    scope (voice, OpenClaw) must refuse the desk token. If ``accept`` were ignored
    and every configured token were honoured everywhere, this is the test that
    turns red."""
    monkeypatch.setenv(_DESK, "desk-only")
    monkeypatch.setenv(_BROAD, "broad")
    assert _check(app, "Bearer desk-only") == ("Invalid bearer token", 401)


def test_the_broad_token_still_opens_the_desk_route_during_transition(app, monkeypatch):
    monkeypatch.setenv(_BROAD, "broad")
    assert _check(app, "Bearer broad", accept=_DESK_ACCEPT) is None


def test_with_both_configured_either_opens_the_desk_route(app, monkeypatch):
    monkeypatch.setenv(_DESK, "desk-only")
    monkeypatch.setenv(_BROAD, "broad")
    assert _check(app, "Bearer desk-only", accept=_DESK_ACCEPT) is None
    assert _check(app, "Bearer broad", accept=_DESK_ACCEPT) is None
    assert _check(app, "Bearer neither", accept=_DESK_ACCEPT) == ("Invalid bearer token", 401)


# ── fail-closed shapes ────────────────────────────────────────────────────────


def test_a_desk_only_install_leaves_default_routes_disabled_not_open(app, monkeypatch):
    """Only the desk token set: voice/OpenClaw have NO accepted token configured,
    so they must answer 503 — not accept the desk token, and not open."""
    monkeypatch.setenv(_DESK, "desk-only")
    assert _check(app, "Bearer desk-only") == (
        "test surface disabled: GENESIS_MCP_HTTP_TOKEN not configured",
        503,
    )


def test_no_accepted_token_configured_is_503_naming_every_candidate(app):
    assert _check(app, "Bearer x", accept=_DESK_ACCEPT) == (
        "test surface disabled: GENESIS_DESK_TOKEN or GENESIS_MCP_HTTP_TOKEN not configured",
        503,
    )


def test_the_default_503_text_is_byte_identical_to_before(app):
    """Voice and OpenClaw tests assert this exact body; the default scope must not
    change it."""
    assert _check(app, "Bearer x") == (
        "test surface disabled: GENESIS_MCP_HTTP_TOKEN not configured",
        503,
    )


def test_an_unset_candidate_is_never_matched_by_an_empty_credential(app, monkeypatch):
    """An unconfigured token is "", and ``compare_digest(b"", b"")`` is True. A
    loop that compared against every NAME rather than every CONFIGURED value would
    let ``Bearer `` through the desk route whenever the desk token is unset."""
    monkeypatch.setenv(_BROAD, "broad")
    assert _check(app, "Bearer ", accept=_DESK_ACCEPT) == ("Invalid bearer token", 401)


def test_a_whitespace_only_desk_token_is_unconfigured(app, monkeypatch):
    monkeypatch.setenv(_DESK, "   ")
    assert _check(app, "Bearer    ", accept=_DESK_ACCEPT)[1] == 503
    assert _check(app, "Bearer ", accept=_DESK_ACCEPT)[1] == 503


def test_a_missing_header_is_401(app, monkeypatch):
    monkeypatch.setenv(_DESK, "desk-only")
    assert _check(app, None, accept=_DESK_ACCEPT) == (
        "Missing or invalid Authorization header",
        401,
    )


def test_a_non_ascii_header_is_still_a_401_not_a_500(app, monkeypatch):
    monkeypatch.setenv(_DESK, "desk-only")
    monkeypatch.setenv(_BROAD, "broad")
    assert _check(app, "Bearer d\xe9sk", accept=_DESK_ACCEPT) == ("Invalid bearer token", 401)


def test_an_empty_accept_is_refused_loudly(app):
    """A caller passing no names would otherwise get a 503 naming nothing."""
    with pytest.raises(ValueError):
        _check(app, "Bearer x", accept=())


def test_a_bare_string_accept_is_refused_not_iterated(app, monkeypatch):
    """The missing-comma tuple ("NAME") is a str. Iterated, it reads
    single-character env names, so an ambient one-letter variable would
    authenticate. Measured by review: with ``_`` set, ``Bearer <its value>`` was
    authorized. It must raise instead."""
    monkeypatch.setenv("_", "/usr/bin/python3")
    with pytest.raises(ValueError):
        _check(app, "Bearer /usr/bin/python3", accept="GENESIS_DESK_TOKEN")


# ── the transition notice: a broad-token desk client is told, once ────────────


@pytest.fixture()
def desk_api(monkeypatch):
    import threading

    from genesis.dashboard.routes import desk_api as mod

    monkeypatch.setattr(mod, "_broad_token_noticed", threading.Event())
    return mod


def _notice(app, desk_api, header):
    with app.test_request_context(headers={"Authorization": header}):
        desk_api._note_broad_token_on_desk()


def test_a_broad_token_desk_client_is_told_once(app, desk_api, monkeypatch, caplog):
    monkeypatch.setenv(_DESK, "desk-only")
    monkeypatch.setenv(_BROAD, "broad")
    _notice(app, desk_api, "Bearer broad")
    _notice(app, desk_api, "Bearer broad")
    notices = [r for r in caplog.records if "GENESIS_DESK_TOKEN" in r.getMessage()]
    assert len(notices) == 1


def test_a_desk_token_client_is_not_told(app, desk_api, monkeypatch, caplog):
    monkeypatch.setenv(_DESK, "desk-only")
    monkeypatch.setenv(_BROAD, "broad")
    _notice(app, desk_api, "Bearer desk-only")
    assert not [r for r in caplog.records if "GENESIS_DESK_TOKEN" in r.getMessage()]


def test_presented_bearer_is_never_matches_an_unconfigured_token(app, monkeypatch):
    from genesis.dashboard.auth import presented_bearer_is

    with app.test_request_context(headers={"Authorization": "Bearer "}):
        assert presented_bearer_is(_DESK) is False


# ── #2110: one reader, one notion of "configured" ─────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("abc", "abc"),
        ("  abc  ", "abc"),
        ("   ", ""),
        ("", ""),
    ],
)
def test_bearer_token_strips(monkeypatch, raw, expected):
    monkeypatch.setenv(_BROAD, raw)
    assert bearer_token(_BROAD) == expected


def test_bearer_token_unset_is_empty():
    assert bearer_token(_BROAD) == ""


@pytest.fixture()
def mcp_server():
    return private_module("_genesis_mcp_server_bearer_scope", _SERVER_SCRIPT)


@pytest.mark.parametrize(
    "cli,env,expected",
    [
        (None, "abc", "abc"),
        (None, "  abc  ", "abc"),
        # The #2110 divergence: a whitespace-only value used to BECOME the MCP
        # secret while the Flask side treated the same value as unconfigured.
        (None, "   ", ""),
        ("cli-tok", "env-tok", "cli-tok"),
        ("  cli-tok ", None, "cli-tok"),
        # A whitespace-only CLI value is unconfigured, so the environment decides.
        ("   ", "env-tok", "env-tok"),
        (None, None, ""),
    ],
)
def test_the_mcp_transport_resolves_its_token_like_every_other_reader(
    mcp_server,
    monkeypatch,
    cli,
    env,
    expected,
):
    if env is None:
        monkeypatch.delenv(_BROAD, raising=False)
    else:
        monkeypatch.setenv(_BROAD, env)
    assert mcp_server._resolve_http_auth_token(cli) == expected


def test_the_mcp_transport_never_accepts_the_desk_token(mcp_server, monkeypatch):
    """The desk token must not reach the MCP tool surface on installs that run it
    over HTTP. The transport resolves only its own variable."""
    monkeypatch.setenv(_DESK, "desk-only")
    assert mcp_server._resolve_http_auth_token(None) == ""


# ── the boot warning names exactly the disabled surfaces ──────────────────────


def test_no_warning_when_the_broad_token_is_set():
    from genesis.hosting.standalone import v1_token_warning

    assert v1_token_warning("broad", "") is None
    assert v1_token_warning("broad", "desk") is None


def test_with_neither_token_every_surface_is_named():
    from genesis.hosting.standalone import v1_token_warning

    msg = v1_token_warning("", "")
    for surface in ("/v1/voice/*", "/v1/chat/completions", "/v1/desk/chat/completions"):
        assert surface in msg


def test_a_desk_only_install_is_not_told_its_desk_route_is_down():
    """Naming the desk route here would send the operator hunting for a fault
    that does not exist — the desk token alone enables it."""
    from genesis.hosting.standalone import v1_token_warning

    msg = v1_token_warning("", "desk")
    assert "/v1/voice/*" in msg and "/v1/chat/completions" in msg
    assert "/v1/desk/chat/completions" not in msg


def test_the_same_value_in_both_slots_is_called_out():
    """Scope is a property of the VALUE. With the broad value copied into the desk
    slot, the "desk" token opens every /v1 route, and nothing else would say so."""
    from genesis.hosting.standalone import v1_token_warning

    msg = v1_token_warning("same", "same")
    assert msg and "not scoped" in msg


def test_the_boot_path_uses_the_pure_warning():
    """Wiring: the pure function is only worth anything if registration calls
    it. Read the registration source rather than trusting it."""
    import inspect

    from genesis.hosting import standalone

    src = inspect.getsource(standalone.StandaloneAdapter)
    assert "v1_token_warning(" in src
    assert 'bearer_token("GENESIS_DESK_TOKEN")' in src


# ── the chokepoint lock ───────────────────────────────────────────────────────

_TOKEN_NAMES = frozenset({"GENESIS_MCP_HTTP_TOKEN", "GENESIS_DESK_TOKEN"})
# The only calls allowed to receive a token NAME: they read through the one reader.
_SANCTIONED_READERS = frozenset({"bearer_token", "presented_bearer_is"})


def _token_name_misuses(source: str) -> list[int]:
    """Line numbers where a token name is used in a context that could READ it raw.

    Decided by CONTEXT, not by spelling. A regex over spellings passed six ways
    past a review (``"X" in os.environ``, ``from os import getenv``,
    ``.setdefault``, ``.pop``, a multi-line call, a bare ``environ.get``). Here the
    name as an exact string is allowed in only two places:
      * an argument to a sanctioned reader (``bearer_token("…")``);
      * a member of a literal tuple/list/set of names (the ``accept`` tuples, the
        MCP process's propagation allowlist) — a list of names reads nothing.
    Everything else — a call to anything else, a subscript, a comparison, an
    assignment to a variable — is flagged. Docstrings and messages never match,
    because they are never EXACTLY the bare name.

    Known limit, stated rather than hidden: a name built dynamically
    (``"GENESIS_" + "DESK_TOKEN"``) or taken from a loop over a tuple is not an
    exact constant and is not seen.
    """
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and node.value in _TOKEN_NAMES):
            continue
        parent = parents.get(node)
        if isinstance(parent, (ast.Tuple, ast.List, ast.Set)):
            continue
        if isinstance(parent, ast.Call) and node in parent.args:
            func = parent.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _SANCTIONED_READERS:
                continue
        hits.append(node.lineno)
    return hits


def test_every_read_of_a_bearer_token_goes_through_the_one_reader():
    """#2110 happened because three call sites each remembered (or forgot) to
    strip. A convention decays one instance at a time, so the obligation lives in
    ``genesis.env.bearer_token`` and this enumerates every Python file under
    ``src/`` and ``scripts/`` for a use that routes around it."""
    offenders = []
    scanned = 0
    for root in ("src", "scripts"):
        for path in (_REPO_ROOT / root).rglob("*.py"):
            scanned += 1
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if rel == "src/genesis/env.py":
                continue
            offenders += [
                f"{rel}:{n}" for n in _token_name_misuses(path.read_text(encoding="utf-8"))
            ]
    # Guard-the-guard: an empty scan passes vacuously.
    assert scanned > 100, f"scanned only {scanned} files; the enumeration is broken"
    assert not offenders, "read these through genesis.env.bearer_token: " + "; ".join(offenders)


@pytest.mark.parametrize(
    "snippet",
    [
        'import os\nos.environ.get("GENESIS_MCP_HTTP_TOKEN", "")',
        "import os\nos.getenv('GENESIS_DESK_TOKEN')",
        'import os\nos.environ["GENESIS_MCP_HTTP_TOKEN"]',
        'import os\nok = "GENESIS_MCP_HTTP_TOKEN" in os.environ',
        'from os import environ\nenviron.get("GENESIS_DESK_TOKEN")',
        'from os import getenv\ngetenv("GENESIS_MCP_HTTP_TOKEN")',
        'import os\nos.environ.get(\n    "GENESIS_MCP_HTTP_TOKEN",\n)',
        'import os\nos.environ.setdefault("GENESIS_DESK_TOKEN", "")',
        'import os\nos.environ.pop("GENESIS_MCP_HTTP_TOKEN", None)',
        'import os\nNAME = "GENESIS_DESK_TOKEN"\nos.environ.get(NAME)',
    ],
)
def test_the_lock_catches_every_raw_read_spelling(snippet):
    """Negative control: each spelling a review found past the earlier regex."""
    assert _token_name_misuses(snippet), snippet


@pytest.mark.parametrize(
    "snippet",
    [
        'from genesis.env import bearer_token\nbearer_token("GENESIS_DESK_TOKEN")',
        'check(accept=("GENESIS_DESK_TOKEN", "GENESIS_MCP_HTTP_TOKEN"))',
        'VARS = {"GENESIS_MCP_HTTP_TOKEN", "OTHER"}',
        'presented_bearer_is("GENESIS_DESK_TOKEN")',
        'log("GENESIS_MCP_HTTP_TOKEN not configured")',
        '"""Uses GENESIS_DESK_TOKEN."""',
    ],
)
def test_the_lock_allows_the_sanctioned_forms(snippet):
    """Positive control: without it, a lock that flagged everything would pass the
    repo scan only by failing — and would be deleted by the next person it annoys."""
    assert not _token_name_misuses(snippet), snippet


# ── the secrets panel reports what the gate will do (#2466) ────────────────────


@pytest.mark.parametrize("name", [_BROAD, _DESK])
@pytest.mark.parametrize(
    ("value", "expected"),
    [("   ", "not_set"), ("\t\n", "not_set"), ("", "not_set"), ("tok", "configured"), ("  tok ", "configured")],
)
def test_the_secrets_panel_never_calls_a_blank_bearer_token_configured(monkeypatch, name, value, expected):
    """A whitespace-only bearer token is unset at its reader, so the panel must not show
    it as configured while the route answers 503."""
    from genesis.dashboard.routes.secrets import _key_status

    monkeypatch.setenv(name, value)
    assert _key_status(name) == expected


def test_the_secrets_panel_keeps_raw_truthiness_for_other_keys(monkeypatch):
    """Provider readers do not strip, so a whitespace-only provider key is still live at
    runtime; the panel must keep calling it configured (and so keep checking its breaker)."""
    from genesis.dashboard.routes.secrets import _key_status

    monkeypatch.setenv("API_KEY_EXAMPLE_FOR_TEST", "   ")
    assert _key_status("API_KEY_EXAMPLE_FOR_TEST") == "configured"


@pytest.mark.parametrize("name", [_BROAD, _DESK])
def test_a_bearer_token_can_be_revoked_from_the_secrets_editor(name):
    """Both bearer tokens are optional: unset disables their routes. The editor only
    lets an operator clear a key the template ships commented, so a leaked desk
    token must be revocable there, not only by hand-editing secrets.env."""
    from genesis.dashboard.routes.secrets import _parse_example_file

    by_key = {d.key: d for d in _parse_example_file()}
    assert by_key[name].is_optional_override is True
    assert by_key[name].is_sensitive is True
