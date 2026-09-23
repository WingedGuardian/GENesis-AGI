"""The disclosure sites must REDACT when no dashboard password is configured.

``auth.is_authenticated()`` returns ``True`` when ``DASHBOARD_PASSWORD`` is
unset — "auth disabled". For a GATE (``if not is_authenticated(): 401``) that
is the documented open-by-default behaviour and nothing here changes it.

For a DISCLOSURE decision it is an inversion: the flag that chooses
redact-vs-reveal is flipped to REVEAL on exactly the installs that have no
credential. ``GET /api/genesis/secrets`` therefore serves every API key in
plaintext to an unauthenticated caller by default, and the two backup routes
serve filesystem paths and a NAS username the same way.

Three sites share the shape, found by classifying every ``is_authenticated()``
call site rather than by grepping for the ones that look dangerous:
``routes/secrets.py`` (key values), ``routes/backup.py`` twice (tier-2 target,
and the config paths/user).

Each case is asserted in BOTH configurations, because the defect is invisible
from a configured install — which is how it survived: a session measured the
endpoint on a box that had a password set, saw empty values, and recorded the
behaviour as correct.
"""

from __future__ import annotations

import pytest
from flask import Flask

# Importing for the side effect of registering routes on the shared blueprint.
import genesis.dashboard.routes.backup  # noqa: F401
import genesis.dashboard.routes.secrets  # noqa: F401
from genesis.dashboard._blueprint import blueprint

_FAKE_KEY = "fc-fake-not-a-real-credential-0123456789"


@pytest.fixture()
def app(monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    # A known sensitive value so the assertion does not depend on whatever the
    # test runner happens to have inherited.
    monkeypatch.setenv("FIRECRAWL_API_KEY", _FAKE_KEY)

    flask_app = Flask(__name__)
    flask_app.secret_key = "test-secret-key"
    flask_app.register_blueprint(blueprint)
    return flask_app


def _sensitive_values(payload: dict) -> list[str]:
    """Every non-empty value on an entry the registry marks sensitive."""
    return [
        key["value"]
        for group in payload.get("groups", [])
        for key in group.get("keys", [])
        if key.get("is_sensitive") and key.get("value")
    ]


def test_secrets_values_are_redacted_when_no_password_is_set(app):
    """The defect: a passwordless install serves plaintext keys to anyone."""
    resp = app.test_client().get("/api/genesis/secrets")
    assert resp.status_code == 200

    payload = resp.get_json()
    # Guard-the-guard: the fixture must actually have produced a sensitive
    # entry, or "no values leaked" would pass against an empty registry.
    entries = [
        k for g in payload.get("groups", []) for k in g.get("keys", []) if k.get("is_sensitive")
    ]
    assert entries, "fixture built no sensitive entries — the assertion below would be vacuous"

    # COUNT, never the list. pytest rewrites the assertion and prints the
    # compared objects, so `assert leaked == []` emits the very credentials
    # this test exists to catch — into CI logs, at the moment it fires.
    leaked = len(_sensitive_values(payload))
    assert leaked == 0, (
        f"{leaked} sensitive value(s) served without a credential; "
        "a passwordless install must redact, not reveal"
    )


def test_secrets_values_are_served_to_an_authenticated_session(app, monkeypatch):
    """The mirror: the fix must not degrade to 'redact always'."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")

    client = app.test_client()
    assert client.post("/api/genesis/auth/login", json={"password": "pw"}).status_code == 200

    payload = client.get("/api/genesis/secrets").get_json()
    assert _FAKE_KEY in _sensitive_values(payload), (
        "an authenticated session must still see configured values"
    )


def test_secrets_values_are_redacted_when_a_password_is_set_but_no_session(app, monkeypatch):
    """The third cell: configured install, anonymous caller.

    This is what a password-protected box already did correctly, and the
    reason the defect went unnoticed — measuring the endpoint HERE shows empty
    values and reads as proof the design is sound. Pinned so a future change
    cannot fix the passwordless cell by breaking this one.
    """
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")

    payload = app.test_client().get("/api/genesis/secrets").get_json()
    assert len(_sensitive_values(payload)) == 0


def test_a_session_flag_without_a_password_still_proves_nothing(app):
    """The fourth cell, and the one that matters for the predicate's shape.

    ``has_verified_credential`` checks for a configured password BEFORE it
    reads the session, so a session marked authenticated on an install that
    has no credential cannot unlock disclosure. Order matters: reading the
    session first would make the flag sufficient on its own.
    """
    # Set the flag DIRECTLY — that is the point. The login route now refuses to
    # mint one here, so going through it would prove nothing about the
    # predicate's own ordering, which is what this test pins.
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["authenticated"] = True

    payload = client.get("/api/genesis/secrets").get_json()
    assert len(_sensitive_values(payload)) == 0, (
        "a session flag must not unlock values where no password exists to verify against"
    )


def test_login_mints_no_session_when_no_password_is_configured(app):
    """A passwordless install must not hand out proof it cannot check.

    ``check_password`` returns True when no password is set, so ``auth_login``
    used to stamp ``session["authenticated"]`` — permanent, 30 days — for any
    POST at all. The cookie outlives the configuration change, so an operator
    who later sets a password (the remediation this very change recommends)
    inherits a pre-harvested session that satisfies the disclosure predicate.
    """
    client = app.test_client()
    resp = client.post("/api/genesis/auth/login", json={"password": "anything"})

    with client.session_transaction() as sess:
        assert not sess.get("authenticated"), (
            "a session was minted on an install with no credential to verify against"
        )
    assert resp.status_code != 401  # not a failure — there is nothing to fail


def test_a_harvested_session_does_not_survive_setting_a_password(app, monkeypatch):
    """The attack the previous test prevents, asserted end to end."""
    client = app.test_client()
    client.post("/api/genesis/auth/login", json={"password": "anything"})

    # Operator now secures the install.
    monkeypatch.setenv("DASHBOARD_PASSWORD", "real-password")

    payload = client.get("/api/genesis/secrets").get_json()
    assert len(_sensitive_values(payload)) == 0, (
        "a session harvested before the password existed still unlocks values"
    )


def test_rotating_the_password_evicts_disclosure_for_old_sessions(app, monkeypatch):
    """Changing the password after a suspected compromise must mean something."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "first")
    client = app.test_client()
    assert client.post("/api/genesis/auth/login", json={"password": "first"}).status_code == 200
    assert _sensitive_values(client.get("/api/genesis/secrets").get_json()), (
        "guard-the-guard: the session must first be able to see values"
    )

    monkeypatch.setenv("DASHBOARD_PASSWORD", "second")
    payload = client.get("/api/genesis/secrets").get_json()
    assert len(_sensitive_values(payload)) == 0, (
        "a session bound to the OLD password still discloses after rotation"
    )


def test_backup_config_paths_are_redacted_when_no_password_is_set(app, monkeypatch):
    """Same inversion, lower severity: filesystem paths and a NAS username."""
    monkeypatch.setenv("GENESIS_BACKUP_LOCAL_PATH", "/srv/fake-backup-target")
    monkeypatch.setenv("GENESIS_BACKUP_NAS_USER", "fake-nas-user")

    payload = app.test_client().get("/api/genesis/backup/config").get_json()

    assert not payload.get("local_path"), "backup path disclosed without a credential"
    assert not payload.get("nas_user"), "NAS username disclosed without a credential"


def test_backup_config_paths_are_served_to_an_authenticated_session(app, monkeypatch):
    """The mirror for the backup pair."""
    monkeypatch.setenv("DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("GENESIS_BACKUP_LOCAL_PATH", "/srv/fake-backup-target")

    client = app.test_client()
    assert client.post("/api/genesis/auth/login", json={"password": "pw"}).status_code == 200

    payload = client.get("/api/genesis/backup/config").get_json()
    assert payload.get("local_path") == "/srv/fake-backup-target"


def test_gates_are_untouched_when_no_password_is_set(app):
    """Nobody's ACCESS is narrowed — only what gets DISCLOSED changes.

    A gate keyed on the same predicate must still pass on a passwordless
    install, or this change has quietly become a lockout.
    """
    # A gated MUTATION route stays reachable on a passwordless install.
    resp = app.test_client().post("/api/genesis/recon/watchlist", json={"repo": "owner/name"})
    assert resp.status_code != 401, "a passwordless install must not start refusing gated mutations"
