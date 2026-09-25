"""The ``PUT /api/genesis/secrets`` write contract.

The empty string used to mean TWO things — "I did not change this field" and
"clear this setting" — and no client could express the difference. An editor
that had not been served the current value (values are withheld from a caller
that has not proved it is the operator) therefore submitted an untouched field
as ``""``, which the route read as *delete the override*.

The contract now says it explicitly:

==========================  ===================================================
key ABSENT from ``keys``    no change
``null``                    clear (optional overrides only)
``""`` or whitespace        422 — ambiguous, refused
non-empty string            set
==========================  ===================================================

**This file is the first executable coverage of that payload shape** — nothing
tested the HTTP-level meaning of an empty value before, which is exactly how the
ambiguity survived. Two rows FLIP here: ``null`` on an optional override was a
422 and is now a 200, and ``""`` on one was a 200-that-cleared and is now a 422.

The keys are DERIVED from the shipped registry rather than hardcoded, so the
tests stay true on any install and cannot silently rot when
``secrets.env.example`` changes.
"""

from __future__ import annotations

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint
from genesis.dashboard.routes import secrets as secrets_mod

#: Excluded as the "required key" sample only because it carries extra
#: field-specific validation that would muddy a test about the write contract.
_SPECIAL_CASED = {"TELEGRAM_ALLOWED_USERS"}


@pytest.fixture()
def registry_keys():
    """One clearable key and one that may never be cleared, from the real registry.

    The asserts are guard-the-guard: if the registry ever loses either category
    every test below would pass VACUOUSLY (there would be nothing to refuse, or
    nothing to clear), so the failure lands here instead of going green.
    """
    clearable = sorted(secrets_mod._OPTIONAL_OVERRIDE_KEYS)
    required = sorted(
        secrets_mod._KNOWN_KEYS - secrets_mod._OPTIONAL_OVERRIDE_KEYS - _SPECIAL_CASED
    )
    assert clearable, "registry exposes no optional-override keys — clear tests would be vacuous"
    assert required, "registry exposes no required keys — refusal tests would be vacuous"
    return clearable[0], required[0]


@pytest.fixture()
def secrets_file(tmp_path, monkeypatch, registry_keys):
    """An isolated secrets.env with both sample keys already assigned."""
    clearable, required = registry_keys
    path = tmp_path / "secrets.env"
    path.write_text(
        "# test fixture\n"
        f"{clearable}=http://configured.invalid\n"
        f"{required}=an-existing-credential\n"
    )
    monkeypatch.setattr(secrets_mod, "secrets_path", lambda: path)
    # The route mutates os.environ so the dashboard status refreshes immediately.
    # setenv (rather than delenv) is what makes monkeypatch record these names:
    # delenv records NOTHING when the var is absent, so a route-set value would
    # leak into the rest of the suite.
    monkeypatch.setenv(clearable, "sentinel")
    monkeypatch.setenv(required, "sentinel")
    return path


@pytest.fixture()
def client(secrets_file):
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    # NOT app.config["TESTING"], which re-raises instead of producing the real
    # error response — the 500-vs-400 test below depends on the real one.
    app.config["TESTING"] = False
    app.config["PROPAGATE_EXCEPTIONS"] = False
    return app.test_client()


def _active(path, key):
    """Uncommented assignments for *key* — what the environment would pick up."""
    return [ln for ln in path.read_text().splitlines() if ln.startswith(f"{key}=")]


# ── null means CLEAR ────────────────────────────────────────────────────────


def test_null_clears_an_optional_override(client, secrets_file, registry_keys):
    clearable, _ = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {clearable: None}})

    assert resp.status_code == 200
    assert _active(secrets_file, clearable) == []
    # Commented out, NOT written as `KEY=` — a bare assignment still shadows
    # genesis.yaml with an empty string, which is worse than the value it replaced.
    assert f"# {clearable}=http://configured.invalid" in secrets_file.read_text()


def test_null_never_writes_the_literal_string_None(client, secrets_file, registry_keys):
    """The corruption the writer's guard exists for, asserted at the HTTP edge.

    `KEY=None` reads back as '' through ``_key_value`` while os.environ still
    holds it, so the dashboard would show the key unset while the live process
    kept shadowing genesis.yaml.
    """
    clearable, _ = registry_keys
    client.put("/api/genesis/secrets", json={"keys": {clearable: None}})

    assert f"{clearable}=None" not in secrets_file.read_text()
    assert f"{clearable}=" not in [ln.strip() for ln in secrets_file.read_text().splitlines()]


def test_the_response_says_which_keys_were_cleared(client, registry_keys):
    clearable, _ = registry_keys

    cleared = client.put("/api/genesis/secrets", json={"keys": {clearable: None}})
    assert cleared.get_json()["cleared"] == [clearable]

    was_set = client.put("/api/genesis/secrets", json={"keys": {clearable: "1.25"}})
    assert was_set.get_json()["cleared"] == []


def test_a_required_key_cannot_be_cleared_and_the_message_says_so(client, registry_keys):
    _, required = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {required: None}})

    assert resp.status_code == 422
    assert "cannot be cleared" in str(resp.get_json())


def test_an_unknown_key_reports_the_unknown_key_not_the_clear(client):
    """Ordering matters: {"NOPE": null} is an unknown KEY, not an un-clearable one."""
    resp = client.put("/api/genesis/secrets", json={"keys": {"NOPE_NOT_A_KEY": None}})

    assert resp.status_code == 422
    # test_timezone_settings.py asserts this literal too — keep the substring.
    assert "Unknown key" in str(resp.get_json())
    assert "cannot be cleared" not in str(resp.get_json())


# ── the empty string is AMBIGUOUS and refused ───────────────────────────────


@pytest.mark.parametrize("empty", ["", "   ", "\t"])
def test_an_empty_value_is_refused_rather_than_clearing(client, secrets_file, registry_keys, empty):
    """THE FLIP. This exact request used to answer 200 and delete the override."""
    clearable, _ = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {clearable: empty}})

    assert resp.status_code == 422
    assert "ambiguous" in str(resp.get_json())
    assert _active(secrets_file, clearable) == [f"{clearable}=http://configured.invalid"]


def test_an_empty_value_on_a_required_key_is_still_refused(client, registry_keys):
    _, required = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {required: ""}})

    assert resp.status_code == 422


# ── absent means NO CHANGE ──────────────────────────────────────────────────


def test_a_key_absent_from_the_payload_is_untouched(client, secrets_file, registry_keys):
    clearable, required = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {clearable: "http://new.invalid"}})

    assert resp.status_code == 200
    assert _active(secrets_file, clearable) == [f"{clearable}=http://new.invalid"]
    assert _active(secrets_file, required) == [f"{required}=an-existing-credential"]


def test_a_non_empty_string_sets_the_value(client, secrets_file, registry_keys):
    clearable, _ = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {clearable: "  http://x.invalid  "}})

    assert resp.status_code == 200
    assert _active(secrets_file, clearable) == [f"{clearable}=http://x.invalid"]


# ── type discipline ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("falsy", [0, False])
def test_falsy_non_strings_are_not_a_clear(client, secrets_file, registry_keys, falsy):
    """`val is None`, never `if not val` — 0 and False are falsy and mean nothing."""
    clearable, _ = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {clearable: falsy}})

    assert resp.status_code == 422
    assert "must be a string, or null" in str(resp.get_json())
    assert _active(secrets_file, clearable) == [f"{clearable}=http://configured.invalid"]


@pytest.mark.parametrize("junk", [7, 1.5, [], {}, ["a"]])
def test_non_string_values_are_refused_and_the_message_names_null(client, registry_keys, junk):
    clearable, _ = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {clearable: junk}})

    assert resp.status_code == 422
    assert "or null to clear" in str(resp.get_json())


# ── envelope ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("body", [[1, 2], "hi", 7, [{"keys": {}}]])
def test_a_truthy_non_dict_body_is_a_400_not_a_500(client, body):
    """These survived ``get_json(silent=True) or {}`` and raised AttributeError."""
    resp = client.put("/api/genesis/secrets", json=body)

    assert resp.status_code == 400
    assert "JSON object" in str(resp.get_json())


@pytest.mark.parametrize("body", [{"keys": {}}, {"keys": [1]}, {"keys": None}, {}])
def test_a_missing_or_malformed_keys_object_is_a_400(client, body):
    resp = client.put("/api/genesis/secrets", json=body)

    assert resp.status_code == 400
    assert "'keys' object" in str(resp.get_json())


# ── batching ────────────────────────────────────────────────────────────────


def test_one_invalid_key_writes_nothing_at_all(client, secrets_file, registry_keys):
    """A partial write would be the worst regression available here.

    The route accumulates every error and returns BEFORE touching the file, so a
    batch carrying one bad value leaves the other keys exactly as they were.
    """
    clearable, required = registry_keys
    before = secrets_file.read_text()

    resp = client.put(
        "/api/genesis/secrets",
        json={"keys": {clearable: "http://valid.invalid", required: ""}},
    )

    assert resp.status_code == 422
    assert secrets_file.read_text() == before


def test_a_valid_clear_batched_with_an_unknown_key_writes_nothing(
    client, secrets_file, registry_keys
):
    clearable, _ = registry_keys
    before = secrets_file.read_text()

    resp = client.put(
        "/api/genesis/secrets",
        json={"keys": {clearable: None, "NOPE_NOT_A_KEY": "x"}},
    )

    assert resp.status_code == 422
    assert secrets_file.read_text() == before


# ── line-break injection ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "sep",
    ["\r", "\n", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", " ", " "],
)
def test_no_separator_can_smuggle_a_second_assignment(client, secrets_file, registry_keys, sep):
    """A value may not contain ANY line separator — not just ``\\n``.

    MEASURED before this was fixed, with a bare CR: the value is written as one
    physical line, but ``Path.read_text()`` (universal newlines) AND python-dotenv
    — which loads this file with ``override=True`` at startup — both treat a lone
    ``\\r`` as a line boundary. So ``KEY=good\\rOTHER=x`` parsed as TWO
    assignments, and the second had passed neither ``_KNOWN_KEYS`` nor any
    per-key rule: an arbitrary environment variable written straight past the
    registry allowlist, activating on the next restart.

    The check is derived from ``str.splitlines()`` rather than enumerating
    characters, so this parametrisation is a sample of a closed set rather than
    the definition of it — a separator nobody listed is still refused.
    """
    clearable, _ = registry_keys
    before = secrets_file.read_text()

    resp = client.put(
        "/api/genesis/secrets",
        json={"keys": {clearable: f"http://ok.invalid{sep}INJECTED_NOT_IN_REGISTRY=1"}},
    )

    assert resp.status_code == 422, f"{sep!r} was accepted"
    assert "line break" in str(resp.get_json())
    assert secrets_file.read_text() == before
    assert "INJECTED_NOT_IN_REGISTRY" not in secrets_file.read_text()


def test_a_null_byte_is_still_rejected(client, registry_keys):
    """The control for the other half of the condition."""
    clearable, _ = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {clearable: "a\x00b"}})

    assert resp.status_code == 422


def test_an_ordinary_value_with_inner_spaces_is_still_accepted(client, registry_keys):
    """The negative control: tightening the separator check must not over-block."""
    clearable, _ = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {clearable: "a b\tc"}})

    assert resp.status_code == 200, resp.get_json()


# ── the refusal must not route the operator into a second refusal ───────────

def test_an_empty_required_key_is_not_told_to_send_null(client, registry_keys):
    """The empty-value arm is reached by BOTH kinds of key, and most have no
    unset state — telling those operators to "send null" is a two-step dead end,
    because the very next request refuses it."""
    _, required = registry_keys

    empty = client.put("/api/genesis/secrets", json={"keys": {required: ""}})
    assert empty.status_code == 422
    assert "null" not in str(empty.get_json()), (
        "an empty value on a key that cannot be cleared must not advertise null"
    )
    assert "no unset state" in str(empty.get_json())

    # ...and the advice it DOES give has to be the advice that works.
    assert client.put(
        "/api/genesis/secrets", json={"keys": {required: "a-real-value"}}
    ).status_code == 200


def test_an_empty_clearable_key_IS_told_to_send_null(client, registry_keys):
    """The other side of the branch — the control that keeps it from being vacuous."""
    clearable, _ = registry_keys
    resp = client.put("/api/genesis/secrets", json={"keys": {clearable: ""}})

    assert resp.status_code == 422
    assert "send null to clear it" in str(resp.get_json())


def test_a_shadowing_empty_assignment_is_repairable(client, secrets_file, registry_keys):
    """`KEY=` reports as `not_set` but is still an assignment that shadows the yaml.

    `_key_status` filters ''/'None'/'NA', so the dashboard shows such a key as
    unset while it keeps overriding genesis.yaml — the one-way door the
    optional-override mechanism exists to prevent. The server must accept a clear
    for it, and the Clear button must therefore NOT be gated on status.
    """
    clearable, _ = registry_keys
    secrets_file.write_text(f"# fixture\n{clearable}=\n")

    resp = client.put("/api/genesis/secrets", json={"keys": {clearable: None}})

    assert resp.status_code == 200
    assert _active(secrets_file, clearable) == []
    assert f"# {clearable}=" in secrets_file.read_text()
