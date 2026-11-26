"""Post-open callbacks must not donate evidence to another call's witness."""

import os
import signal

import pytest

from tests import _live_writes as audit
from tests.test_live_writes import home as home
from tests.test_live_writes_fd import _write


@pytest.mark.parametrize("use_signal", [False, True])
@pytest.mark.parametrize("protected_cwd", [False, True])
def test_post_open_callback_has_independent_primary_evidence(home, monkeypatch, use_signal, protected_cwd):
    witness = audit._W
    allowed = home.parent / "allowed"
    monkeypatch.chdir(home / "genesis" if protected_cwd else allowed)
    directory = witness.native(home / ".genesis", os.O_RDONLY | os.O_DIRECTORY)
    original_parent = witness._parent
    previous_handler = signal.getsignal(signal.SIGUSR1)
    calls = 0
    callback_scopes = []

    def callback(*args):
        callback_scopes.append(witness._scope())
        fd = witness.native("callback", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=directory)
        try:
            os.write(fd, b"owned callback contents")
        finally:
            os.close(fd)

    def parent(*args):
        nonlocal calls
        value = original_parent(*args)
        calls += 1
        if calls == 2:
            if use_signal:
                os.kill(os.getpid(), signal.SIGUSR1)
            else:
                callback()
        return value

    monkeypatch.setattr(witness, "_parent", parent)
    signal.signal(signal.SIGUSR1, callback)
    try:
        audit._S.hits.clear()
        _write(allowed)
    finally:
        signal.signal(signal.SIGUSR1, previous_handler)
        os.close(directory)
    assert (home / ".genesis/callback").read_bytes() == b"owned callback contents"
    assert (allowed / "owned").read_bytes() == b"owned synthetic bytes"
    if protected_cwd:
        assert any(row[1].startswith("relative os.open") and row[3].endswith("/callback")
                   for row in audit._S.hits)
        assert not any(row[2] == "write" and row[3].endswith("/owned") for row in audit._S.hits)
    # A saved native alias outside an active native call retains only primary
    # coverage. Detachment must not imply a new universal alias guarantee.
    assert callback_scopes == [None]
