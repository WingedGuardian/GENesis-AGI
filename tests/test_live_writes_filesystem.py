"""Native filesystem outcomes and the report-only audit, all in an owned home."""

import errno
import os

import pytest

from tests import _live_writes as audit
from tests.test_live_writes import _hits
from tests.test_live_writes import home as home


@pytest.mark.parametrize("raw", [False, True])
@pytest.mark.parametrize("entry", ["file", "dangling"])
def test_refused_exclusive_open_has_no_access(home, raw, entry):
    target = home / ".genesis" / "existing"
    if entry == "file":
        target.write_bytes(b"owned")
    else:
        target.symlink_to(home / ".genesis" / "absent")
    audit._S.hits.clear()
    with pytest.raises(FileExistsError):
        if raw:
            os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        else:
            target.open("x").close()
    assert not audit._S.hits
    assert audit._S.faults == 0


@pytest.mark.parametrize("credential", [False, True])
def test_refused_nofollow_open_has_no_access(home, credential):
    target = home / ("genesis/secrets.env" if credential else ".genesis/state")
    target.write_bytes(b"owned")
    alias = home.parent / "allowed" / "alias"
    alias.symlink_to(target)
    audit._S.hits.clear()
    flags = os.O_RDONLY if credential else os.O_WRONLY | os.O_TRUNC
    with pytest.raises(OSError) as error:
        os.close(os.open(alias, flags | os.O_NOFOLLOW))
    assert error.value.errno == errno.ELOOP
    assert not audit._S.hits
    assert audit._S.faults == 0
    assert target.read_bytes() == b"owned"


@pytest.mark.parametrize("extra", [os.O_CREAT | os.O_WRONLY, os.O_TRUNC | os.O_RDWR])
def test_opath_ignored_write_flags_do_not_access_data(home, extra):
    target = home / "genesis" / "secrets.env"
    target.write_bytes(b"owned")
    audit._S.hits.clear()
    os.close(os.open(target, os.O_PATH | extra))
    assert not audit._S.hits
    assert audit._S.faults == 0


def test_fd_exclusive_wrapper_still_records_real_write(home):
    target = home / ".genesis" / "state"
    target.write_bytes(b"before")
    descriptor = os.open(target, os.O_WRONLY)
    try:
        audit._S.hits.clear()
        with os.fdopen(descriptor, "x", closefd=False) as handle:
            handle.write("after!")
    finally:
        os.close(descriptor)
    assert target.read_bytes() == b"after!"
    assert ("~/.genesis", "write", "state") in _hits()
    assert audit._S.faults == 0


@pytest.mark.parametrize("kind", ["exclusive", "nofollow"])
def test_relative_raw_open_cannot_use_cwd_entry_for_suppression(home, monkeypatch, kind):
    guessed = home / ".genesis"
    actual = home.parent / "allowed"
    if kind == "exclusive":
        (guessed / "entry").write_bytes(b"guessed")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    else:
        (guessed / "entry").symlink_to(guessed / "other")
        (actual / "entry").write_bytes(b"before")
        flags = os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW
    directory = os.open(actual, os.O_RDONLY | os.O_DIRECTORY)
    monkeypatch.chdir(guessed)
    try:
        audit._S.hits.clear()
        descriptor = os.open("entry", flags, dir_fd=directory)
        os.write(descriptor, b"changed")
        os.close(descriptor)
    finally:
        os.close(directory)
    assert (actual / "entry").read_bytes() == b"changed"
    assert any(label.startswith("relative os.open") for label, _, _ in _hits())
    assert audit._S.faults == 0


@pytest.mark.parametrize("operation", [os.rename, os.link])
def test_missing_source_cannot_change_destination(home, operation):
    destination = home / ".genesis" / "destination"
    audit._S.hits.clear()
    with pytest.raises(FileNotFoundError):
        operation(home.parent / "allowed" / "missing", destination)
    assert not destination.exists()
    assert not audit._S.hits
    assert audit._S.faults == 0


def test_existing_link_destination_neither_exposes_nor_writes(home):
    source = home / ".genesis" / "source"
    source.write_bytes(b"owned")
    destination = home.parent / "allowed" / "occupied"
    destination.symlink_to(home / ".genesis" / "absent")
    audit._S.hits.clear()
    with pytest.raises(FileExistsError):
        os.link(source, destination)
    assert not audit._S.hits
    assert audit._S.faults == 0


@pytest.mark.parametrize("symlink", [False, True])
def test_same_inode_rename_changes_neither_name(home, symlink):
    source = home / ".genesis" / "source"
    if symlink:
        source.symlink_to(home / ".genesis" / "absent")
    else:
        source.write_bytes(b"owned")
    destination = home / ".genesis" / "destination"
    os.link(source, destination, follow_symlinks=False)
    audit._S.hits.clear()
    os.rename(source, destination)
    assert source.lstat().st_ino == destination.lstat().st_ino
    assert not audit._S.hits
    assert audit._S.faults == 0


def test_protected_source_hardlink_exposes_shared_bytes(home):
    source = home / ".genesis" / "source"
    source.write_bytes(b"before")
    alias = home.parent / "allowed" / "alias"
    audit._S.hits.clear()
    os.link(source, alias)
    alias.write_bytes(b"after")
    assert source.read_bytes() == b"after"
    assert ("~/.genesis", "os.link source", "source") in _hits()
    assert audit._S.faults == 0


@pytest.mark.parametrize("operation", [os.rename, os.link])
def test_named_destination_with_dir_fd_still_records(home, operation):
    allowed = home.parent / "allowed"
    (allowed / "source").write_bytes(b"owned")
    source_fd = os.open(allowed, os.O_RDONLY | os.O_DIRECTORY)
    destination_fd = os.open(home / ".genesis", os.O_RDONLY | os.O_DIRECTORY)
    try:
        audit._S.hits.clear()
        operation("source", "destination", src_dir_fd=source_fd, dst_dir_fd=destination_fd)
    finally:
        os.close(source_fd)
        os.close(destination_fd)
    assert (home / ".genesis" / "destination").read_bytes() == b"owned"
    assert ("~/.genesis", f"os.{operation.__name__}", "destination") in _hits()
    assert audit._S.faults == 0


@pytest.mark.parametrize("follow", [False, True])
def test_link_symlink_source_has_explicit_conservative_exposure(home, follow):
    source = home / ".genesis" / "source"
    source.write_bytes(b"owned")
    allowed = home.parent / "allowed"
    symlink = allowed / "link"
    symlink.symlink_to(source)
    audit._S.hits.clear()
    os.link(symlink, allowed / "alias", follow_symlinks=follow)
    assert ("~/.genesis", "os.link source", "source") in _hits()
    assert audit._S.faults == 0


@pytest.mark.parametrize("event", ["open", "rename", "link"])
def test_metadata_uncertainty_preserves_known_destination(home, monkeypatch, event):
    target = home / ".genesis" / "destination"
    source = home.parent / "allowed" / "source"
    source.write_bytes(b"owned")
    original = audit.os.lstat

    def unknown(path, *args, **kwargs):
        if os.fspath(path) == str(target if event == "open" else source):
            raise OSError(errno.EIO, "owned metadata fault")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(audit.os, "lstat", unknown)
    audit._S.hits.clear()
    if event == "open":
        with target.open("x") as handle:
            handle.write("owned")
    else:
        getattr(os, event)(source, target)
    assert target.read_bytes() == b"owned"
    assert ("~/.genesis", "write" if event == "open" else f"os.{event}", "destination") in _hits()
    assert audit._S.faults >= 1


@pytest.mark.parametrize("operation", [os.link, os.rename])
def test_unknown_source_resolution_preserves_absolute_destination(home, monkeypatch, operation):
    allowed = home.parent / "allowed"
    (allowed / "source").write_bytes(b"owned")
    destination = home / ".genesis" / "destination"
    directory = os.open(allowed, os.O_RDONLY | os.O_DIRECTORY)

    def unavailable_cwd():
        raise FileNotFoundError("owned unavailable cwd")

    try:
        monkeypatch.setattr(audit.os, "getcwd", unavailable_cwd)
        audit._S.hits.clear()
        operation("source", destination, src_dir_fd=directory)
        assert destination.read_bytes() == b"owned"
        assert ("~/.genesis", f"os.{operation.__name__}", "destination") in _hits()
        assert audit._S.faults >= 1
    finally:
        os.close(directory)
