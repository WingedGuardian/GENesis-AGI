"""The dashboard file browser must not serve credentials, and must not be open.

Two independent defects, which is why there are two halves here.

**The routes carry no auth at all.** ``routes/files.py`` contains no
``is_authenticated`` / ``_auth_or_403`` call of any kind. The blueprint gate
exempts ``/api/*`` and the app-level gate exempts GET, so every file route
answers an anonymous caller — including on a password-protected install.

**The credential denylist misses the files that matter.** ``_BLOCKED_NAMES``
holds ``credentials.json``; the real Claude Code file is ``.credentials.json``
and the leading dot defeats the exact-name match. ``internal_api_token`` is not
listed at all — and that token is the CSRF-immune bearer
``check_api_mutation_auth`` accepts, so reading it converts an anonymous caller
into one that passes the mutation gate.

The two halves cover different installs and neither is sufficient alone:
gating is inert on a passwordless install (``is_authenticated`` returns True
there by design), so the credential vocabulary is what narrows DISCLOSURE in
that configuration; the gate is what protects a configured one.

Scope, so these tests are not read as proving more than they do. The vocabulary
is a disclosure control over a MEASURED set of spellings, not a closed category,
and it has no bearing on the write routes — a passwordless install's
write/create/rename/delete/upload stay open by construction, which is a property
of declining to configure a credential rather than something this module can fix.
"""

from __future__ import annotations

import pytest
from flask import Flask

import genesis.dashboard.auth as auth_mod
import genesis.dashboard.routes.files as files_mod
from genesis.dashboard._blueprint import blueprint


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret-key"
    flask_app.register_blueprint(blueprint)
    return flask_app


# ── Half one: the credential denylist ────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        ".credentials.json",  # the real Claude Code file; the dot defeats the list
        "internal_api_token",  # the bearer the mutation gate accepts
    ],
)
def test_credential_files_are_never_allowed(tmp_path, monkeypatch, name):
    """Refused on their own merits, with no reference to who is asking.

    This half has to hold WITHOUT auth, because on a passwordless install the
    gate below is inert — that is the configuration where these files were
    being served.
    """
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(files_mod, "_ALLOWED_ROOTS", [root])

    target = root / name
    target.write_text("pretend-credential")

    assert files_mod._is_allowed(target) is False, f"{name} is readable through the file browser"


def test_a_token_named_file_is_refused_by_component_not_by_exact_name(tmp_path, monkeypatch):
    """The denylist is exact-match, so a list of names is a list of misses.

    An exact-name rule fails on every spelling nobody enumerated — which is how
    ``.credentials.json`` slipped past an entry for ``credentials.json``. The
    rule this pins is on the path COMPONENT, so an unanticipated spelling is
    refused by construction rather than by having been thought of.
    """
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(files_mod, "_ALLOWED_ROOTS", [root])

    for name in ("some_api_token", "oauth_credential.bak", "TOKEN.txt"):
        target = root / name
        target.write_text("x")
        assert files_mod._is_allowed(target) is False, f"{name} was allowed"


def test_ordinary_files_are_still_readable(tmp_path, monkeypatch):
    """Guard-the-guard: the rule must not refuse everything.

    Without this, a denylist that returned False unconditionally would satisfy
    every assertion above.
    """
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(files_mod, "_ALLOWED_ROOTS", [root])

    ordinary = root / "notes.md"
    ordinary.write_text("hello")
    assert files_mod._is_allowed(ordinary) is True


# ── Half two: the routes are gated ───────────────────────────────────


_READ_ROUTES = [
    "/api/genesis/files?path=.",
    "/api/genesis/files/read?path=x",
    "/api/genesis/files/download?path=x",
]


@pytest.mark.parametrize("url", _READ_ROUTES)
def test_file_routes_refuse_an_anonymous_caller_when_a_password_is_set(app, monkeypatch, url):
    """A configured install must not serve its filesystem to the network."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")

    resp = app.test_client().get(url)
    # Assert the AUTH refusal specifically. `_is_allowed` also answers 403 for a
    # path outside the allowed roots, so a bare status check would pass even if
    # the gate were absent entirely — the test would be measuring containment.
    body = resp.get_json(silent=True) or {}
    assert resp.status_code == 403 and body.get("error") == "authentication required", (
        f"{url} answered {resp.status_code} {body} to an anonymous caller "
        "on a password-protected install"
    )


@pytest.mark.parametrize("url", _READ_ROUTES)
def test_file_routes_still_answer_a_passwordless_install(app, url):
    """No access is narrowed where there is no credential to present.

    ``is_authenticated()`` returns True when no password is configured, so the
    gate is deliberately inert here — this asserts that, because silently
    turning a passwordless install's file browser into 403s is exactly the
    regression this change must not cause.
    """
    resp = app.test_client().get(url)
    # Discriminate by the ERROR, not the status. `_is_allowed` also answers 403
    # for a path outside the allowed roots, and these URLs carry a placeholder
    # path whose resolution depends on the process cwd — so a bare
    # `!= 403` passes or fails for reasons that have nothing to do with auth.
    body = resp.get_json(silent=True) or {}
    # One-sided on its own: `get_json(silent=True) or {}` yields {} for a
    # non-JSON 500, which satisfies the `!=` below, so a route that crashes
    # outright would read as correctly inert.
    assert resp.status_code != 500, f"{url} raised on a passwordless install: {resp.data[:200]!r}"
    assert body.get("error") != "authentication required", (
        f"{url} refused a passwordless install for AUTH — the gate must be inert there"
    )


def test_file_routes_serve_an_authenticated_session(app, monkeypatch, tmp_path):
    """The mirror: a logged-in operator keeps working access."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes.md").write_text("hello")
    monkeypatch.setattr(files_mod, "_ALLOWED_ROOTS", [root])

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True

    resp = client.get(f"/api/genesis/files/read?path={root / 'notes.md'}")
    assert resp.status_code == 200, "an authenticated operator lost file access"


def test_every_file_route_is_gated(app, monkeypatch):
    """Enumerated, not sampled — a route added later must not arrive ungated.

    The parametrised cases above cover the three read routes by hand. This
    walks the url map instead, so a NEW file route is caught by construction
    rather than by someone remembering to extend a list.
    """
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    client = app.test_client()

    ungated: list[str] = []
    checked: list[str] = []
    for rule in app.url_map.iter_rules():
        if not str(rule).startswith("/api/genesis/files"):
            continue
        method = "GET" if "GET" in rule.methods else sorted(rule.methods - {"HEAD", "OPTIONS"})[0]
        checked.append(f"{method} {rule}")
        resp = client.open(
            str(rule).replace("<path:", "<").replace("<", "x").replace(">", ""), method=method
        )
        body = resp.get_json(silent=True) or {}
        # The AUTH refusal specifically — a containment 403 would otherwise
        # count as "gated" and this enumeration would certify a route that has
        # no auth check at all.
        if not (resp.status_code == 403 and body.get("error") == "authentication required"):
            ungated.append(f"{method} {rule} -> {resp.status_code} {body}")

    # The DENOMINATOR, asserted. Without this the loop is vacuous the moment its
    # prefix stops matching: every rule `continue`s, `ungated` stays empty, and
    # the enumeration reports success having checked nothing. Probed — with the
    # prefix perturbed to `/api/genesis/fileZ` the assertion below still passes
    # while zero routes were examined.
    assert len(checked) == 8, f"expected 8 file routes, enumerated {len(checked)}: {checked}"
    assert not ungated, "file routes answering an anonymous caller: " + "; ".join(ungated)


# ── The vocabulary's measured scope ──────────────────────────────────


@pytest.mark.parametrize(
    "name",
    [
        # The two that motivated the change.
        ".credentials.json",
        "internal_api_token",
        # Shapes the FIRST draft served, because it matched only exact stems
        # and `_`-suffixes. Each of these spellings was found holding a real
        # credential during the review sweep.
        "backup_passphrase.env",
        "service_creds.env",
        "app_passwd",
        "control.key",
        # Spellings the `_`-only rule could not see.
        "api-token",
        ".git-credentials",
        "refresh-token.txt",
        "tokens.json",
        "creds",
        "id_ed25519",
        ".netrc",
    ],
)
def test_credential_vocabulary_refuses_the_measured_set(tmp_path, monkeypatch, name):
    """Every spelling here was READ off this install or off the review of it.

    The first draft of this rule caught the two files that motivated it and
    missed nineteen further credential files in a sweep of the allowed roots —
    which is the exact failure mode (a list of the spellings someone thought
    of) that the rule exists to replace. These lock the widened vocabulary so
    a later simplification cannot quietly re-open it.
    """
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(files_mod, "_ALLOWED_ROOTS", [root])
    target = root / name
    target.write_text("pretend-credential")
    assert files_mod._is_allowed(target) is False, f"{name} is readable through the file browser"


@pytest.mark.parametrize(
    "name",
    [
        "tokenizer.md",  # substring, not a credential
        "token-optimizer",  # a product NAME that leads with the stem
        "analyze-token-usage.py",
        "token-creation.md",
        "notes.md",
    ],
)
def test_ordinary_names_survive_the_widened_vocabulary(tmp_path, monkeypatch, name):
    """Guard-the-guard, and it is load-bearing rather than ceremonial.

    A draft that matched the stem ANYWHERE in a component scored 4.226% against
    87,219 files under the allowed roots, and 3,591 of those were one installed
    plugin whose name contains "token" — an entire tree made unbrowsable. The
    shipped rule anchors on the LAST word of each dot-segment precisely so
    `token-optimizer` stays readable while `api-token` does not. Without these
    cases that distinction can be refactored away and only the false-negative
    half would fail.
    """
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(files_mod, "_ALLOWED_ROOTS", [root])
    target = root / name
    target.write_text("ordinary")
    assert files_mod._is_allowed(target) is True, f"{name} was wrongly refused"


@pytest.mark.parametrize("name", [".env.local", ".env.production", ".env.development"])
def test_dotenv_variants_are_refused(tmp_path, monkeypatch, name):
    """A credential extension is not always the LAST one.

    The dotenv convention puts the environment AFTER the extension, so an
    `endswith('.env')` test sees `.local` and passes the file straight through —
    and `_BLOCKED_NAMES` carries only bare `.env`. These are the spellings a
    project is most likely to actually have.
    """
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setattr(files_mod, "_ALLOWED_ROOTS", [root])
    target = root / name
    target.write_text("API_KEY=x")
    assert files_mod._is_allowed(target) is False, f"{name} is readable through the file browser"


def test_internal_bearer_reaches_the_file_routes(app, monkeypatch, tmp_path):
    """The two gates on one request must agree about who is trusted.

    ``check_api_mutation_auth`` accepts the internal bearer explicitly and
    origin-independently, because a browser attacker cannot read a 0600 file to
    forge it. A sibling gate that consults only the session cookie would refuse
    the very caller the mutation gate just blessed — so a trusted machine caller
    would pass one gate and be 403'd by the next, on the same request.
    """
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    root = tmp_path / "root"
    root.mkdir()
    (root / "notes.md").write_text("hello")
    monkeypatch.setattr(files_mod, "_ALLOWED_ROOTS", [root])

    token = auth_mod.get_or_create_internal_api_token()
    resp = app.test_client().get(
        f"/api/genesis/files/read?path={root / 'notes.md'}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, "a trusted machine caller lost file access"


def test_a_wrong_bearer_is_still_refused(app, monkeypatch):
    """Guard-the-guard: accepting the header is not accepting any header.

    Without this, `has_internal_bearer` could return True on the mere PRESENCE
    of an Authorization header and every assertion above would still pass.
    """
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    resp = app.test_client().get(
        "/api/genesis/files?path=.", headers={"Authorization": "Bearer not-the-token"}
    )
    body = resp.get_json(silent=True) or {}
    assert resp.status_code == 403 and body.get("error") == "authentication required"


# ── The bearer header is BYTES, not str ──────────────────────────────


@pytest.mark.parametrize(
    ("label", "url", "method", "want"),
    [
        ("read gate", "/api/genesis/files?path=.", "GET", 403),
        ("mutation gate", "/api/genesis/files/write", "PUT", 401),
    ],
)
def test_a_high_byte_bearer_is_refused_not_a_500(app, monkeypatch, label, url, method, want):
    """A header byte must not turn a security gate into a stack trace.

    WSGI decodes headers as latin-1, and ``hmac.compare_digest`` REFUSES
    non-ASCII ``str`` operands — it raises ``TypeError``. Comparing the raw
    header string therefore returned a 500 for any header carrying a high byte,
    on the widest surface in the app. Still fail-closed, but a spammable 500
    where a refusal belongs, and a stack trace per request.

    ``check_bearer_token`` already solved this by comparing BYTES with
    ``surrogateescape``; this pins that both bearer gates do the same. The
    status codes differ by design (the read gate mirrors ``references.py`` at
    403, the mutation gate answers 401) — what matters is that neither is 500.
    """
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    app.before_request(auth_mod.check_api_mutation_auth)

    resp = app.test_client().open(
        url, method=method, headers={"Authorization": "Bearer \xff\xfe"}
    )

    assert resp.status_code != 500, f"{label} raised instead of refusing"
    assert resp.status_code == want, f"{label} answered {resp.status_code}"


def test_both_bearer_gates_share_one_implementation(monkeypatch):
    """The two gates must not be able to disagree about who is trusted.

    This is the defect the extraction exists to prevent, so it is asserted
    structurally rather than by testing two behaviours and hoping they stay in
    step: the mutation gate calls ``has_internal_bearer`` rather than carrying
    its own copy of the compare.
    """
    import inspect

    source = inspect.getsource(auth_mod.check_api_mutation_auth)
    assert "has_internal_bearer()" in source, (
        "the mutation gate no longer calls the shared helper"
    )
    assert "compare_digest" not in source, (
        "the mutation gate has re-grown its own bearer compare — the two gates "
        "can now drift apart, which is exactly what the helper prevents"
    )
