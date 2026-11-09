"""The explicit forwarder reads the existing P3 store without provisioning."""

import json

import pytest

from genesis import caller_credentials as credentials
from genesis.caller_context import CallerContext


def test_forwarder_headers_preserve_distinct_identity_and_resolve(tmp_path):
    directory = tmp_path / "credentials"
    credentials.ensure_caller_credentials(directory=directory)
    before = {path.name: path.read_bytes() for path in directory.iterdir()}
    resolver = credentials.CallerResolver.load(directory)
    callers = [CallerContext(dispatched=False, supervised=True, session_id="s-" + str(i),
                             transcript_id="t-" + str(i), origin="owner") for i in range(2)]
    for caller in callers:
        headers = credentials.forwarder_headers(caller, directory=directory)
        assert set(headers) == {"Authorization", "X-Genesis-Caller"}
        resolved = resolver.resolve(headers["Authorization"].removeprefix("Bearer "),
                                    json.loads(headers["X-Genesis-Caller"]))
        assert resolved == caller
    assert {path.name: path.read_bytes() for path in directory.iterdir()} == before


@pytest.mark.parametrize("kind", ["missing", "mode", "symlink", "token"])
def test_forwarder_never_creates_repairs_or_follows_a_bad_store(tmp_path, kind):
    directory = tmp_path / "credentials"
    if kind != "missing":
        credentials.ensure_caller_credentials(directory=directory)
        token = directory / "forwarder"
        if kind == "mode":
            token.chmod(0o644)
        elif kind == "symlink":
            target = directory / "saved"
            token.rename(target)
            token.symlink_to(target)
        else:
            token.write_text("invalid")
    with pytest.raises(credentials.CredentialStoreError):
        credentials.forwarder_headers(CallerContext(), directory=directory)
    if kind == "missing":
        assert not directory.exists()
    elif kind == "mode":
        assert (directory / "forwarder").stat().st_mode & 0o777 == 0o644
    elif kind == "symlink":
        assert (directory / "forwarder").is_symlink()
    else:
        assert (directory / "forwarder").read_text() == "invalid"


@pytest.mark.parametrize("caller", [CallerContext(profile="codex"),
                                    CallerContext(profile="external"),
                                    CallerContext(session_id="../unsafe"),
                                    CallerContext(dispatched=True, supervised=False, origin="owner")])
def test_forwarder_refuses_labels_profiles_and_invalid_identity_before_read(tmp_path, monkeypatch, caller):
    directory = tmp_path / "credentials"
    credentials.ensure_caller_credentials(directory=directory)

    def unexpected_read(*args, **kwargs):
        raise AssertionError("Invalid caller reached credential read")

    monkeypatch.setattr(credentials, "_read_token", unexpected_read)
    with pytest.raises(credentials.CredentialStoreError):
        credentials.forwarder_headers(caller, directory=directory)
