"""Shared MCP service caller credentials: provisioning and the stateless resolver."""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path
from unittest import mock

import pytest

import genesis.caller_credentials as cc
from genesis.caller_context import CallerContext
from genesis.mcp.external_profiles import ExternalProfile

FOREGROUND = {"dispatched": False, "supervised": False, "transcript_id": "t-1"}


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.lstat(path).st_mode)


def _token(path: Path) -> str:
    return path.read_text()


@pytest.fixture
def store(tmp_path: Path) -> Path:
    return tmp_path / "mcp_credentials"


@pytest.fixture
def resolver(store: Path) -> cc.CallerResolver:
    cc.ensure_caller_credentials(("external", "validator", "interactive"), store)
    return cc.CallerResolver.load(store)


def _near_miss(token: str) -> str:
    return token[:-1] + ("A" if token[-1] != "A" else "B")


# ── provisioning ─────────────────────────────────────────────────────────


def test_ensure_creates_private_files_in_private_dir(store: Path) -> None:
    result = cc.ensure_caller_credentials(directory=store)
    assert result == {"forwarder": "created", "external-external": "created"}
    assert _mode(store) == 0o700
    for name in result:
        assert _mode(store / name) == 0o600
        assert cc._TOKEN_RE.fullmatch(_token(store / name))
    assert _token(store / "forwarder") != _token(store / "external-external")
    assert sorted(p.name for p in store.iterdir()) == sorted(result)  # no temp file left


def test_ensure_never_overwrites(store: Path) -> None:
    cc.ensure_caller_credentials(directory=store)
    before = {p.name: _token(p) for p in store.iterdir()}
    again = cc.ensure_caller_credentials(directory=store)
    assert again == {"forwarder": "present", "external-external": "present"}
    assert {p.name: _token(p) for p in store.iterdir()} == before


def test_ensure_keeps_a_file_that_appears_concurrently(store: Path) -> None:
    store.mkdir(mode=0o700)
    placed = "x" * 43
    (store / "forwarder").write_text(placed)
    os.chmod(store / "forwarder", 0o600)
    assert cc._create_once(store / "forwarder") is False
    assert _token(store / "forwarder") == placed
    assert [p.name for p in store.iterdir()] == ["forwarder"]


def test_ensure_tightens_loose_modes_without_rewriting(store: Path) -> None:
    cc.ensure_caller_credentials(directory=store)
    before = _token(store / "forwarder")
    os.chmod(store, 0o755)
    os.chmod(store / "forwarder", 0o644)
    assert cc.ensure_caller_credentials(directory=store)["forwarder"] == "present"
    assert _mode(store) == 0o700
    assert _mode(store / "forwarder") == 0o600
    assert _token(store / "forwarder") == before


@pytest.mark.parametrize("content", ["", "short", "x" * 43 + "!", "é" * 43])
def test_ensure_refuses_a_malformed_file_and_keeps_it(store: Path, content: str) -> None:
    store.mkdir(mode=0o700)
    (store / "forwarder").write_text(content)
    os.chmod(store / "forwarder", 0o600)
    with pytest.raises(cc.CredentialStoreError):
        cc.ensure_caller_credentials(directory=store)
    assert (store / "forwarder").read_text() == content


def test_ensure_refuses_a_symlink(store: Path, tmp_path: Path) -> None:
    store.mkdir(mode=0o700)
    target = tmp_path / "elsewhere"
    target.write_text("x" * 43)
    (store / "forwarder").symlink_to(target)
    with pytest.raises(cc.CredentialStoreError):
        cc.ensure_caller_credentials(directory=store)
    assert target.read_text() == "x" * 43


def test_interactive_is_provisioned_only_by_name(store: Path) -> None:
    assert "external-interactive" not in cc.ensure_caller_credentials(directory=store)
    assert (
        cc.ensure_caller_credentials(("interactive",), store)["external-interactive"] == "created"
    )
    with pytest.raises(ValueError):
        cc.ensure_caller_credentials(("nonesuch",), store)


def test_no_secret_is_logged_or_printed(store: Path, caplog, capsys, monkeypatch) -> None:
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("GENESIS_HOME", str(store.parent))
    monkeypatch.setattr(
        sys, "argv", ["genesis", "mcp-credentials", "ensure", "--profile", "validator"]
    )
    from genesis.__main__ import main

    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    files = sorted(p for p in store.iterdir())
    assert [p.name for p in files] == ["external-validator", "forwarder"]
    os.chmod(store, 0o755)  # force the warning path too
    cc.ensure_caller_credentials(("validator",), store)
    text = out + caplog.text
    assert "created: " in out
    for path in files:
        assert _token(path) not in text


# ── resolver: credential checks ──────────────────────────────────────────


def test_missing_credential_is_rejected(resolver: cc.CallerResolver) -> None:
    for value in (None, "", b""):
        assert resolver.resolve(value, FOREGROUND) == cc.Rejected(cc.MISSING_CREDENTIAL)


def test_wrong_credentials_get_one_indistinguishable_reason(
    resolver: cc.CallerResolver, store: Path
) -> None:
    forwarder = _token(store / "forwarder")
    external = _token(store / "external-external")
    wrong = [
        _near_miss(forwarder),
        _near_miss(external),
        "y" * 43,
        forwarder + "x",
        forwarder[:-1],
        "é" * 43,
        12345,
        ["list"],
        "x" * 10_000,
    ]
    results = {repr(resolver.resolve(value, FOREGROUND)) for value in wrong}
    assert results == {repr(cc.Rejected(cc.INVALID_CREDENTIAL))}
    assert forwarder not in repr(resolver) and external not in repr(resolver)


def test_every_secret_is_compared_in_constant_time(
    resolver: cc.CallerResolver, store: Path
) -> None:
    forwarder = _token(store / "forwarder")
    with mock.patch.object(cc.hmac, "compare_digest", wraps=cc.hmac.compare_digest) as spy:
        result = resolver.resolve(forwarder, FOREGROUND)
    assert isinstance(result, CallerContext)
    assert spy.call_count == 4  # forwarder + three profiles: no early exit on the first match
    with mock.patch.object(cc.hmac, "compare_digest", return_value=False) as stub:
        assert resolver.resolve(forwarder, FOREGROUND) == cc.Rejected(cc.INVALID_CREDENTIAL)
    assert stub.call_count == 4


def test_bytes_credential_is_accepted(resolver: cc.CallerResolver, store: Path) -> None:
    token = _token(store / "external-external").encode("ascii")
    assert resolver.resolve(token, None).profile == "external"


def test_absent_forwarder_file_resolves_nothing(store: Path) -> None:
    cc.ensure_caller_credentials(directory=store)
    forwarder = _token(store / "forwarder")
    (store / "forwarder").unlink()
    loaded = cc.CallerResolver.load(store)
    assert loaded.resolve(forwarder, FOREGROUND) == cc.Rejected(cc.INVALID_CREDENTIAL)


# ── resolver: forwarded identity ─────────────────────────────────────────


def test_forwarder_accepts_validated_identity(resolver: cc.CallerResolver, store: Path) -> None:
    forwarder = _token(store / "forwarder")
    fields = {
        "dispatched": True,
        "supervised": True,
        "session_id": "s-1",
        "origin": "owner",
        "transcript_id": "0b1c-uuid:2",
        "legacy_session_id": "",
    }
    assert resolver.resolve(forwarder, fields) == CallerContext(
        dispatched=True,
        supervised=True,
        session_id="s-1",
        origin="owner",
        transcript_id="0b1c-uuid:2",
        legacy_session_id="",
        profile=None,
    )


def test_two_foreground_sessions_resolve_to_distinct_callers(
    resolver: cc.CallerResolver, store: Path
) -> None:
    forwarder = _token(store / "forwarder")
    a = resolver.resolve(forwarder, {**FOREGROUND, "transcript_id": "t-a", "session_id": "s-a"})
    b = resolver.resolve(forwarder, {**FOREGROUND, "transcript_id": "t-b", "session_id": "s-b"})
    assert isinstance(a, CallerContext) and isinstance(b, CallerContext)
    assert (a.transcript_id, a.session_id) != (b.transcript_id, b.session_id)
    assert not a.dispatched and not b.dispatched


@pytest.mark.parametrize(
    "fields",
    [
        None,
        [("dispatched", False)],
        {"supervised": False},
        {"dispatched": False},
        {"dispatched": 0, "supervised": False},
        {"dispatched": "1", "supervised": False},
        {"dispatched": False, "supervised": 1},
        {**FOREGROUND, "profile": "interactive"},
        {**FOREGROUND, "unknown": "x"},
        {**FOREGROUND, "session_id": 7},
        {**FOREGROUND, "session_id": "x" * 257},
        {**FOREGROUND, "session_id": " s-1"},
        {**FOREGROUND, "session_id": "../x"},
        {**FOREGROUND, "transcript_id": ".."},
        {**FOREGROUND, "transcript_id": "a/b"},
        {**FOREGROUND, "transcript_id": "a\nb"},
        {**FOREGROUND, "transcript_id": "é"},
        {**FOREGROUND, "origin": "root"},
        {**FOREGROUND, "origin": 1},
    ],
)
def test_forwarder_refuses_malformed_identity(
    resolver: cc.CallerResolver, store: Path, fields: object
) -> None:
    result = resolver.resolve(_token(store / "forwarder"), fields)
    assert result == cc.Rejected(cc.INVALID_IDENTITY)


def test_forwarder_id_at_the_bound_is_accepted(resolver: cc.CallerResolver, store: Path) -> None:
    result = resolver.resolve(_token(store / "forwarder"), {**FOREGROUND, "session_id": "x" * 256})
    assert isinstance(result, CallerContext)


# ── resolver: external profiles ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("profile", "dispatched", "supervised"),
    [("external", True, False), ("validator", True, False), ("interactive", False, True)],
)
def test_external_credential_resolves_to_its_profile_only(
    resolver: cc.CallerResolver, store: Path, profile: str, dispatched: bool, supervised: bool
) -> None:
    token = _token(store / f"external-{profile}")
    for empty in (None, {}):
        assert resolver.resolve(token, empty) == CallerContext(
            dispatched=dispatched, supervised=supervised, profile=profile
        )


@pytest.mark.parametrize(
    "fields",
    [FOREGROUND, {"supervised": True}, {"session_id": "s"}, {"profile": "interactive"}, ["x"]],
)
def test_external_credential_cannot_assert_identity(
    resolver: cc.CallerResolver, store: Path, fields: object
) -> None:
    result = resolver.resolve(_token(store / "external-external"), fields)
    assert result == cc.Rejected(cc.IDENTITY_NOT_PERMITTED)


def test_resolver_ignores_the_process_environment(
    resolver: cc.CallerResolver, store: Path, monkeypatch
) -> None:
    for name in ("GENESIS_CC_SESSION", "GENESIS_SESSION_SUPERVISED"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("GENESIS_SESSION_ID", "from-env")
    assert resolver.resolve(_token(store / "external-external"), None).session_id is None
    assert resolver.resolve(_token(store / "forwarder"), FOREGROUND).dispatched is False
    assert resolver.resolve(None, None) == cc.Rejected(cc.MISSING_CREDENTIAL)
    source = Path(cc.__file__).read_text()
    assert "os.environ" not in source and "getenv" not in source and "environb" not in source


def test_every_external_profile_has_a_fixed_caller() -> None:
    # "interactive" arrives in ExternalProfile with the interactive-profile PR;
    # until then it is the one name here that the enum lacks. Equality, so drift
    # in either direction fails.
    pending = {"interactive"} - {p.value for p in ExternalProfile}
    assert {p.value for p in ExternalProfile} | pending == set(cc._PROFILE_CALLERS)
    for profile, caller in cc._PROFILE_CALLERS.items():
        assert caller.profile == profile
        assert caller.session_id is caller.transcript_id is caller.origin is None


# ── loading ──────────────────────────────────────────────────────────────


def test_load_refuses_loose_modes(store: Path) -> None:
    cc.ensure_caller_credentials(directory=store)
    os.chmod(store / "forwarder", 0o640)
    with pytest.raises(cc.CredentialStoreError):
        cc.CallerResolver.load(store)
    os.chmod(store / "forwarder", 0o600)
    os.chmod(store, 0o750)
    with pytest.raises(cc.CredentialStoreError):
        cc.CallerResolver.load(store)


def test_load_creates_nothing(store: Path) -> None:
    with pytest.raises(cc.CredentialStoreError):
        cc.CallerResolver.load(store)
    assert not store.exists()


def test_load_skips_unknown_profile_files_loudly(store: Path, caplog) -> None:
    cc.ensure_caller_credentials(directory=store)
    stray = store / "external-nonesuch"
    stray.write_text("z" * 43)
    os.chmod(stray, 0o600)
    loaded = cc.CallerResolver.load(store)
    assert loaded.resolve("z" * 43, None) == cc.Rejected(cc.INVALID_CREDENTIAL)
    assert "unknown profile" in caplog.text and "z" * 43 not in caplog.text


def test_duplicate_secrets_are_refused(store: Path) -> None:
    cc.ensure_caller_credentials(directory=store)
    (store / "external-external").write_text(_token(store / "forwarder"))
    with pytest.raises(cc.CredentialStoreError):
        cc.CallerResolver.load(store)
    with pytest.raises(cc.CredentialStoreError):
        cc.ensure_caller_credentials(directory=store)


def test_ensure_fails_where_load_would(store: Path) -> None:
    cc.ensure_caller_credentials(directory=store)
    bad = store / "external-validator"
    bad.write_text("not a credential")
    os.chmod(bad, 0o600)
    with pytest.raises(cc.CredentialStoreError):
        cc.ensure_caller_credentials(directory=store)  # validator was not named


def test_non_files_at_a_credential_path_are_refused_cleanly(store: Path) -> None:
    store.mkdir(mode=0o700)
    (store / "forwarder").mkdir()
    fds = len(os.listdir("/proc/self/fd"))
    for _ in range(5):
        with pytest.raises(cc.CredentialStoreError):
            cc.ensure_caller_credentials(directory=store)
    assert len(os.listdir("/proc/self/fd")) == fds
    (store / "forwarder").rmdir()
    os.mkfifo(store / "forwarder")
    with pytest.raises(cc.CredentialStoreError):
        cc.ensure_caller_credentials(directory=store)


def test_an_unusable_store_dir_raises_the_store_error(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.write_text("")
    with pytest.raises(cc.CredentialStoreError):
        cc.ensure_caller_credentials(directory=plain)
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(cc.CredentialStoreError):
        cc.ensure_caller_credentials(directory=link)
    assert list(real.iterdir()) == []


def test_repair_never_touches_an_invalid_or_shared_file(store: Path, tmp_path: Path) -> None:
    store.mkdir(mode=0o700)
    bad = store / "forwarder"
    bad.write_text("not a credential")
    os.chmod(bad, 0o644)
    with pytest.raises(cc.CredentialStoreError):
        cc.ensure_caller_credentials(directory=store)
    assert _mode(bad) == 0o644
    bad.unlink()
    outside = tmp_path / "shared"
    outside.write_text("q" * 43)
    os.chmod(outside, 0o644)
    os.link(outside, bad)
    with pytest.raises(cc.CredentialStoreError):
        cc.ensure_caller_credentials(directory=store)
    assert _mode(outside) == 0o644


@pytest.mark.parametrize("bad", ["é" * 43, b"x" * 43, None, "short"])
def test_constructor_refuses_a_bad_token_with_the_store_error(bad: object) -> None:
    with pytest.raises(cc.CredentialStoreError):
        cc.CallerResolver("x" * 43, {"external": bad})


def test_resolver_cannot_be_pickled(resolver: cc.CallerResolver) -> None:
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(resolver)


def test_cli_error_goes_to_stderr(store: Path, capsys, monkeypatch) -> None:
    store.mkdir(mode=0o700)
    (store / "forwarder").write_text("bad")
    monkeypatch.setenv("GENESIS_HOME", str(store.parent))
    import argparse

    rc = cc._cli_ensure(argparse.Namespace(profile=None))
    captured = capsys.readouterr()
    assert rc == 1 and captured.out == "" and "error:" in captured.err
