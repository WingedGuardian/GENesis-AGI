"""Qualified release selection never permits a different configured executable."""

import hashlib
import os
from pathlib import Path

import pytest

from tests.test_scripts.managed_code_intel_fixture import qualify_bytes
from tests.test_scripts.test_codebase_managed_config import (
    config,
)
from tests.test_scripts.test_codebase_managed_config import (
    managed as managed,
)
from tests.test_scripts.test_codebase_managed_config import (
    staged_configuration as staged_configuration,
)


def lookup(managed):
    operation = getattr(managed, "qualified_release", None)
    assert callable(operation), "release selection must use captured qualified records"
    return operation


def test_shipped_release_policy_is_immutable_and_keeps_old_identity(managed):
    select = lookup(managed)
    records = select.__globals__["RELEASES"]
    assert set(records) == {managed.BUILD}
    assert select(managed.BUILD) == dict(version="0.11.0", worker="v0.11.0-stock-worker")
    with pytest.raises(TypeError):
        records["unqualified"] = {}
    with pytest.raises(TypeError):
        select(managed.BUILD)["version"] = "changed"


@pytest.mark.parametrize("bad", [None, [], {}, "", "latest", "0" * 64])
def test_unknown_release_never_falls_back(managed, bad):
    with pytest.raises(ValueError, match="unsupported"):
        lookup(managed)(bad)


@pytest.mark.parametrize("selected", [0, 1])
def test_settings_accept_each_qualified_build_without_upgrading(managed, tmp_path, monkeypatch, selected):
    digests = qualify_bytes(monkeypatch, lookup(managed), b"first", b"second")
    value = config(tmp_path, managed)
    value["build"] = digests[selected]
    assert managed.validate_settings(value) == value
    path = tmp_path / "settings"
    managed.publish_settings(path, value)
    assert managed.read_settings(path)["build"] == digests[selected]


@pytest.mark.parametrize("selected", [0, 1])
@pytest.mark.parametrize("actual", [0, 1, 2])
def test_execute_requires_exact_configured_build(managed, tmp_path, monkeypatch, selected, actual):
    images = [b"first", b"second", b"unknown"]
    digests = qualify_bytes(monkeypatch, lookup(managed), *images[:2])
    binary = tmp_path / "binary"
    binary.write_bytes(images[actual])
    binary.chmod(0o500)
    if actual != selected:
        with pytest.raises(ValueError, match="executable|build"):
            managed.verified_binary(binary, digests[selected])
    else:
        with managed.verified_binary(binary, digests[selected]) as accepted:
            assert accepted.read() == images[actual]


def test_discovery_is_staging_only_and_returns_retained_identity(managed, tmp_path, monkeypatch):
    select = lookup(managed)
    digest, = qualify_bytes(monkeypatch, select, b"first")
    binary = tmp_path / "binary"
    binary.write_bytes(b"first")
    binary.chmod(0o500)
    opener = managed.open_qualified_binary
    with pytest.raises(ValueError, match="expected|configured"):
        opener(binary)
    stream, actual = opener(binary, execute=False)
    with stream:
        assert actual == digest and stream.read() == b"first"


def test_mismatch_closes_the_opened_descriptor(managed, tmp_path, monkeypatch):
    digests = qualify_bytes(monkeypatch, lookup(managed), b"first", b"second")
    binary = tmp_path / "binary"
    binary.write_bytes(b"second")
    binary.chmod(0o500)
    original = os.fdopen
    streams = []
    def opened(*args, **kwargs):
        stream = original(*args, **kwargs)
        streams.append(stream)
        return stream
    monkeypatch.setattr(os, "fdopen", opened)
    with pytest.raises(ValueError):
        managed.verified_binary(binary, digests[0])
    assert len(streams) == 1 and streams[0].closed


def test_configure_discovers_source_and_copies_the_same_inode(
        managed, staged_configuration, monkeypatch):
    args, settings, state = staged_configuration
    select = lookup(managed)
    digests = qualify_bytes(monkeypatch, select, b"accepted", b"replacement")
    original = managed.open_qualified_binary
    opened = []
    def replace_after_open(path, expected_build=None, *, execute=True):
        stream, digest = original(path, expected_build, execute=execute)
        opened.append((expected_build, execute, digest))
        if path == Path(args.binary):
            replacement = path.with_name("new-source")
            replacement.write_bytes(b"replacement")
            replacement.chmod(0o500)
            replacement.replace(path)
        return stream, digest
    monkeypatch.setitem(managed.configure.__globals__, "open_qualified_binary", replace_after_open)
    managed.configure(args, settings)
    assert managed.read_settings(settings)["build"] == digests[0]
    assert (state / "bin/codebase-memory-mcp").read_bytes() == b"accepted"
    assert opened == [(None, False, digests[0]), (digests[0], True, digests[0])]
    assert hashlib.sha256(Path(args.binary).read_bytes()).hexdigest() == digests[1]
