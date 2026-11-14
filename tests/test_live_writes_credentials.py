"""Owned credential intent, identity and metadata-error controls for the write audit."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from genesis.secrets_location import home_config_location
from tests import _live_writes as audit
from tests.test_live_writes import _hits
from tests.test_live_writes import home as home


@pytest.mark.parametrize(("access", "reads", "writes"), [
    ("r", True, False), ("r+", True, True), ("w", False, True),
    ("w+", True, True), ("a", False, True), ("a+", True, True),
    ("x", False, True), ("x+", True, True),
    (os.O_RDONLY, True, False), (os.O_WRONLY, False, True),
    (os.O_RDWR, True, True), (os.O_CREAT | os.O_RDONLY, True, False),
    pytest.param(3, False, None, marks=pytest.mark.skipif(sys.platform != "linux", reason="Linux ioctl-only mode")),
    pytest.param(getattr(os, "O_PATH", 0), False, False,
                 marks=pytest.mark.skipif(not hasattr(os, "O_PATH"), reason="Linux metadata-only mode")),
])
def test_credential_open_records_independent_access_intents(home, access, reads, writes):
    target = home / "genesis" / "secrets.env"
    if not isinstance(access, str) or not access.startswith("x"):
        target.write_text("owned")
    audit._S.hits.clear()
    if isinstance(access, str):
        with target.open(access) as handle:
            if reads:
                handle.read()
            if writes:
                handle.write("x")
    else:
        descriptor = os.open(target, access)
        try:
            if reads:
                assert os.read(descriptor, 5) == b"owned"
            elif access in (3, getattr(os, "O_PATH", None)):
                with pytest.raises(OSError):
                    os.read(descriptor, 1)
            if writes and access != 3:
                os.write(descriptor, b"x")
        finally:
            os.close(descriptor)
    observed = [(label, operation) for _node, label, operation, path in audit._S.hits
                if path == str(target)]
    assert any(operation == "read" and label.startswith("credential file read")
               for label, operation in observed) is reads
    if writes is not None:
        assert any(operation == "write" for _label, operation in observed) is writes
    assert audit._S.faults == 0


@pytest.mark.parametrize("style", ["replacement", "symlink-rotation", "created-late"])
def test_current_credential_alias_survives_identity_changes(home, monkeypatch, style):
    target = home / "genesis" / "secrets.env"
    allowed = home.parent / "allowed"
    initial = allowed / "initial"
    initial.write_bytes(b"initial")
    if style != "created-late":
        if style == "symlink-rotation":
            target.symlink_to(initial)
        else:
            os.link(initial, target)
        stat = target.stat()
        monkeypatch.setattr(audit._S, "credential_inodes", frozenset({(stat.st_dev, stat.st_ino)}))
    for version in range(2):
        current = allowed / f"current-{version}"
        current.write_bytes(f"owned-{version}".encode())
        if style == "symlink-rotation":
            target.unlink()
            target.symlink_to(current)
        else:
            os.replace(current, target)
        alias = allowed / f"alias-{version}"
        os.link(target.resolve(), alias)
        audit._S.hits.clear()
        audit._S.faults = 0  # measure this read, excluding setup uncertainty on older Python
        assert alias.read_bytes() == f"owned-{version}".encode()
        assert any(label == "credential file read" and operation == "read" and path == str(alias)
                   for _node, label, operation, path in audit._S.hits)
        assert audit._S.faults == 0
    if style != "created-late":
        audit._S.hits.clear()
        assert initial.read_bytes() == b"initial"
        assert any(label == "credential file read" and path == str(initial)
                   for _node, label, _operation, path in audit._S.hits)


def test_descriptor_open_observes_credential_read(home):
    target = home / "genesis" / "secrets.env"
    target.write_text("owned")
    descriptor = os.open(target, os.O_RDONLY)
    try:
        audit._S.hits.clear()
        with os.fdopen(descriptor, "r", closefd=False) as handle:
            assert handle.read() == "owned"
        assert ("credential file read", "read", "secrets.env") in _hits()
    finally:
        os.close(descriptor)


def test_credential_alias_write_is_reported_inside_an_allowed_root(home):
    target = home / "genesis" / "secrets.env"
    target.write_bytes(b"before")
    alias = home.parent / "allowed" / "write-alias"
    os.link(target, alias)
    audit._S.hits.clear()
    alias.write_bytes(b"after")
    assert any(operation == "write" and path == str(alias)
               for _node, _label, operation, path in audit._S.hits)
    assert target.read_bytes() == b"after"


@pytest.mark.parametrize("failure_at", ["candidate", "canonical"])
def test_credential_metadata_failure_is_unknown_without_breaking_read(home, monkeypatch, failure_at):
    target = home / "genesis" / "secrets.env"
    target.write_text("owned")
    alias = home.parent / "allowed" / "stat-alias"
    os.link(target, alias)
    failed = alias if failure_at == "candidate" else target
    stat = audit.os.stat

    def unavailable(path, *args, **kwargs):
        if os.fspath(path) == str(failed):
            raise PermissionError("owned unavailable metadata")
        return stat(path, *args, **kwargs)

    audit._S.hits.clear()
    monkeypatch.setattr(audit.os, "stat", unavailable)
    assert alias.read_text() == "owned"
    assert audit._S.faults == 1


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
@pytest.mark.parametrize("access", ["w", "w+", "a", "a+", os.O_CREAT | os.O_RDONLY, os.O_CREAT | os.O_RDWR])
@pytest.mark.parametrize("credential_index", range(5))
def test_dangling_logical_credential_creation_is_observed(home, monkeypatch, relative, access, credential_index):
    names = [home_config_location(home), home / "genesis/secrets.env",
             home / ".claude/.credentials.json", home / ".config/gh/hosts.yml",
             home / ".genesis/cc_oauth_token.env"]
    monkeypatch.setattr(audit._S, "credential_files", frozenset(map(str, names)))
    logical = names[credential_index]
    logical.parent.mkdir(parents=True, exist_ok=True)
    backing = home.parent / "allowed" / "missing-backing"
    logical.symlink_to(backing)
    if relative:
        monkeypatch.chdir(logical.parent)
    argument = logical.name if relative else str(logical)
    audit._S.hits.clear()
    reads = "+" in access if isinstance(access, str) else True
    if isinstance(access, str):
        with open(argument, access) as handle:
            if reads:
                assert handle.read() == ""
            handle.write("owned")
    else:
        descriptor = os.open(argument, access)
        try:
            assert os.read(descriptor, 1) == b""
            if access & os.O_RDWR:
                os.write(descriptor, b"owned")
        finally:
            os.close(descriptor)
    observed = [(label, operation) for _node, label, operation, path in audit._S.hits
                if path == str(backing)]
    if audit._MISSING_STRICT is True:  # older Python: uncertainty is explicit
        assert observed == []
        assert audit._S.faults == 1
    else:
        assert any(operation == "write" for _label, operation in observed)
        assert any(operation == "read" and label.startswith("credential file read")
                   for label, operation in observed) is reads
        assert audit._S.faults == 0
    assert backing.read_bytes() == (b"" if access == os.O_CREAT | os.O_RDONLY else b"owned")


@pytest.mark.parametrize(("access", "reads"), [("w", False), ("r+", True), (os.O_WRONLY, False), (os.O_RDWR, True)])
def test_unrelated_credential_stat_failure_preserves_known_write(home, monkeypatch, access, reads):
    target = home / ".genesis" / "ordinary-target"
    target.write_text("owned")
    unknown = home / "genesis" / "secrets.env"
    stat = audit.os.stat

    def unavailable(path, *args, **kwargs):
        if os.fspath(path) == str(unknown):
            raise PermissionError("owned unavailable credential metadata")
        return stat(path, *args, **kwargs)

    audit._S.hits.clear()
    with pytest.MonkeyPatch.context() as injection:
        injection.setattr(audit.os, "stat", unavailable)
        if isinstance(access, str):
            with target.open(access) as handle:
                if reads:
                    assert handle.read() == "owned"
                handle.write("changed")
        else:
            descriptor = os.open(target, access)
            try:
                if reads:
                    assert os.read(descriptor, 5) == b"owned"
                os.write(descriptor, b"changed")
            finally:
                os.close(descriptor)
    assert target.read_text().endswith("changed")
    assert ("~/.genesis", "write", "ordinary-target") in _hits()
    assert audit._S.faults == int(reads)


def test_positive_credential_match_wins_over_unrelated_metadata_failure(home, monkeypatch):
    names = frozenset({str(home / "genesis" / "first"), str(home / "genesis" / "second")})
    first, matching = list(names)
    Path(matching).write_text("owned")
    monkeypatch.setattr(audit._S, "credential_files", names)
    alias = home.parent / "allowed" / "positive-alias"
    os.link(matching, alias)
    stat = audit.os.stat

    def unavailable(path, *args, **kwargs):
        if os.fspath(path) == first:
            raise PermissionError("owned unavailable unrelated metadata")
        return stat(path, *args, **kwargs)

    audit._S.hits.clear()
    with pytest.MonkeyPatch.context() as injection:
        injection.setattr(audit.os, "stat", unavailable)
        assert alias.read_text() == "owned"
    assert any(label == "credential file read" and path == str(alias)
               for _node, label, _operation, path in audit._S.hits)
    assert audit._S.faults == 0


@pytest.mark.parametrize("protected", [False, True], ids=["allowed-backing", "protected-backing"])
@pytest.mark.skipif(not hasattr(os.path, "ALLOW_MISSING"), reason="legacy resolution uncertainty tested separately")
@pytest.mark.parametrize(("helper", "failure"), [
    ("lstat", PermissionError), ("lstat", OSError), ("readlink", PermissionError),
])
def test_missing_credential_resolution_failure_is_unknown(home, helper, failure, protected):
    logical = home / "genesis" / "secrets.env"
    backing = (home / ".genesis" if protected else home.parent / "allowed") / "future"
    logical.symlink_to(backing)
    original = getattr(audit.os, helper)

    def unavailable(path, *args, **kwargs):
        if os.fspath(path) == str(logical):
            raise failure("owned unavailable logical credential metadata")
        return original(path, *args, **kwargs)

    audit._S.hits.clear()
    with pytest.MonkeyPatch.context() as injection:
        injection.setattr(audit.os, helper, unavailable)
        with backing.open("w+") as handle:
            assert handle.read() == ""
            handle.write("owned")
    assert backing.read_text() == "owned"
    assert audit._S.faults == 1
    assert any(operation == "write" for _node, _label, operation, _path in audit._S.hits) is protected


def test_legacy_missing_resolution_is_unknown_instead_of_clean(home, monkeypatch):
    logical = home / "genesis" / "secrets.env"
    backing = home.parent / "allowed" / "legacy-future"
    logical.symlink_to(backing)
    monkeypatch.setattr(audit, "_MISSING_STRICT", True, raising=False)
    audit._S.hits.clear()
    with backing.open("w+") as handle:
        assert handle.read() == ""
        handle.write("owned")
    assert audit._S.faults == 1
    assert [row[1:] for row in audit._S.hits] == [
        ("observer fault", "open/judge/FileNotFoundError", "<not captured>")
    ]
    assert backing.read_text() == "owned"


@pytest.mark.skipif(not hasattr(os.path, "ALLOW_MISSING"), reason="legacy resolution uncertainty tested separately")
def test_missing_target_positive_match_wins_over_unrelated_resolution_error(home, monkeypatch):
    names = frozenset({str(home / "genesis/first"), str(home / "genesis/second")})
    first, matching = list(names)
    backing = home.parent / "allowed" / "positive-future"
    Path(matching).symlink_to(backing)
    monkeypatch.setattr(audit._S, "credential_files", names)
    lstat = audit.os.lstat
    called = []

    def unavailable(path, *args, **kwargs):
        if os.fspath(path) == first:
            called.append(True)
            raise PermissionError("owned unrelated resolution failure")
        return lstat(path, *args, **kwargs)

    audit._S.hits.clear()
    with pytest.MonkeyPatch.context() as injection:
        injection.setattr(audit.os, "lstat", unavailable)
        with backing.open("w+") as handle:
            assert handle.read() == ""
            handle.write("owned")
    assert called
    assert {"read", "write"} <= {operation for _node, _label, operation, path in audit._S.hits
                               if path == str(backing)}
    assert audit._S.faults == 0
    assert backing.read_text() == "owned"


@pytest.mark.parametrize("location", ["legacy", "canonical", "canonical-symlink"])
def test_install_records_the_credential_files_by_inode(tmp_path, monkeypatch, location):
    """The inode set is what lets a read under another name be recognised."""
    fake = tmp_path / "home"
    (fake / "genesis").mkdir(parents=True)
    secrets = fake / "genesis" / "secrets.env" if location == "legacy" else home_config_location(fake)
    secrets.parent.mkdir(parents=True, exist_ok=True)
    if location == "canonical-symlink":
        backing = tmp_path / "owned-credential"
        backing.write_text("K=v\n")
        secrets.symlink_to(backing)
    else:
        secrets.write_text("K=v\n")
    st = os.stat(secrets)
    from tests import _live_fence

    monkeypatch.setattr(audit._S, "installed", False)
    monkeypatch.setattr(audit.sys, "addaudithook", lambda hook: None)
    monkeypatch.setattr(_live_fence._S, "installed", True)
    monkeypatch.setattr(_live_fence._S, "real_home", str(fake))
    for name in ("real_home", "allowed_roots", "allowed_files", "classes", "credential_files"):
        monkeypatch.setattr(audit._S, name, getattr(audit._S, name))
    monkeypatch.setattr(audit._S, "credential_inodes", frozenset())
    config = type("C", (), {"option": type("O", (), {"basetemp": None})()})()
    monkeypatch.setattr(audit._S, "hits", {})
    audit.install(config)
    assert str(secrets) in audit._S.credential_files
    assert (st.st_dev, st.st_ino) in audit._S.credential_inodes
    audit._S.hits.clear()
    assert secrets.read_text() == "K=v\n"
    assert any(label == "credential file read" and operation == "read"
               for _node, label, operation, _path in audit._S.hits)
