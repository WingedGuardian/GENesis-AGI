"""The two approved credential shapes use the native scanner's existing walk."""
import json
from pathlib import Path

import pytest

from tests.conftest import private_module
from tests.test_contribution.test_credentials import _key, _url
from tests.test_hooks.test_native_push_privacy import SOURCE, _record, _worker
from tests.test_hooks.test_native_push_privacy import native_world as _native_world


@pytest.fixture
def native_world(tmp_path, request):
    return _native_world.__wrapped__(tmp_path, request)


def _credential(shape):
    return {"akia": _key(), "asia": _key("ASIA"), "url": _url("one:two"),
            "dsn": _url("one:two", user="", scheme="redis")}[shape]


@pytest.mark.parametrize("shape,surface", [
    (shape, surface) for shape in ("akia", "asia", "url", "dsn")
    for surface in ("line", "message", "tag", "path")
    if surface != "path" or shape in ("akia", "asia")
])
def test_existing_native_projections_detect_credentials(native_world, shape, surface):
    repo, env, git, commit, invoke = native_world
    value = _credential(shape)
    oid = commit(value if surface == "message" else "safe",
                 text=value + "\n" if surface == "line" else "safe\n",
                 filename=value if surface == "path" else "sample")
    if surface == "tag":
        raw = f"object {oid}\ntype commit\ntag fixture\ntagger Fixture <fixture@example.invalid> 1 +0000\n\n{value}\n"
        oid = git("hash-object", "-w", "-t", "tag", "--stdin", data=raw.encode()).decode().strip()
    result = _worker(native_world, _record(oid))
    assert result["verdict"] == "TIER_A"
    assert {item[0] for item in result["findings"]} == {"aws-key-id" if shape in ("akia", "asia") else "url-credential"}
    assert value not in json.dumps(result)


@pytest.mark.parametrize("automated", [False, True])
@pytest.mark.parametrize("shape", ["akia", "asia", "url", "dsn"])
def test_installed_chain_refuses_first_and_identical_repeat(native_world, automated, shape):
    repo, env, git, commit, invoke = native_world
    env["GENESIS_AUTOMATED_PUSH"] = "1" if automated else "0"
    value = _credential(shape)
    oid = commit("safe", text=value + "\n")
    for _ in range(2):
        result = invoke(oid)
        assert result.returncode == 2 and b"TIER_A" in result.stderr
        assert value.encode() not in result.stdout + result.stderr
        assert b"Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize("fingerprints", ["missing", "empty", "pathological"])
def test_confirmed_credential_precedes_unavailable_or_slow_fingerprint(native_world, fingerprints):
    repo, env, git, commit, invoke = native_world
    path = Path(env["GENESIS_RELEASE_FINGERPRINTS"])
    if fingerprints == "missing":
        path.unlink()
    else:
        path.write_text("(a+)+$\n" if fingerprints == "pathological" else "")
    # Earlier message/path scan units may already record missing fingerprints.
    oid = commit("safe", text=_key("ASIA") + " " + "a" * 48 + "!\n")
    result = invoke(oid)
    assert result.returncode == 2 and b"TIER_A" in result.stderr
    assert _key("ASIA").encode() not in result.stdout + result.stderr


def test_credential_confirmation_stops_before_later_fingerprint_error(monkeypatch):
    from genesis.contribution import sanitize

    module = private_module("credential_sequence", SOURCE / "scripts/hooks/push_privacy_scan.py")
    monkeypatch.setattr(sanitize, "_check_fingerprints", lambda *a, **kw: pytest.fail("late detector ran"))
    parsed = sanitize._ParsedDiff([], [("opaque", 1, _key())], False, 0)
    assert next(module._checks(parsed, None)).scanner == "aws-key-id"


def test_add_then_scrub_is_still_scanned_and_known_remote_excludes_history(native_world):
    repo, env, git, commit, invoke = native_world
    added = commit("safe", text=_key("ASIA") + "\n")
    scrubbed = commit("safe", parent=added)
    assert _worker(native_world, _record(scrubbed))["verdict"] == "TIER_A"
    assert _worker(native_world, _record(scrubbed, added))["verdict"] == "CLEAN"


def test_private_destination_and_transport_password_are_not_published_content(native_world):
    repo, env, git, commit, invoke = native_world
    oid = commit("safe", text=_key() + "\n")
    assert _worker(native_world, _record(oid), destination="git@github.com:fixture/private.git")["verdict"] == "CLEAN"
    safe = commit("safe")
    destination = "https://fixture:" + "synthetic-password" + "@github.com/fixture/public.git"
    result = _worker(native_world, _record(safe), destination=destination)
    assert result["verdict"] == "CLEAN"
    assert "synthetic-password" not in json.dumps(result)


def test_confirmed_message_precedes_later_binary_refusal(native_world):
    repo, env, git, commit, invoke = native_world
    oid = commit(_key(), text="binary\x00content")
    assert _worker(native_world, _record(oid))["verdict"] == "TIER_A"
