"""Native destination/compatibility regressions; every path is owned by the test."""

import inspect
import os
import pickle
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests import _live_writes as audit
from tests.test_live_writes import home as home


def _clear():
    audit._S.hits.clear()
    audit._S.faults = 0


def _write(directory, name="owned"):
    dfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=dfd)
        try:
            os.write(fd, b"owned synthetic bytes")
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def test_owned_descriptor_does_not_write_to_guessed_protected_cwd(home, monkeypatch):
    monkeypatch.chdir(home / "genesis")
    allowed = home.parent / "allowed"
    _clear()
    _write(allowed)
    assert (allowed / "owned").read_bytes() == b"owned synthetic bytes"
    assert not (home / "genesis/owned").exists()
    assert audit._S.hits == {} and audit._S.faults == 0


def test_protected_descriptor_is_seen_even_when_cwd_is_owned(home, monkeypatch):
    monkeypatch.chdir(home.parent / "allowed")
    _clear()
    _write(home / ".genesis")
    assert (home / ".genesis/owned").read_bytes() == b"owned synthetic bytes"
    assert any(row[1:] == ("~/.genesis", "write", str(home / ".genesis/owned"))
               for row in audit._S.hits)
    assert audit._S.faults == 0


def test_real_protected_write_remains_reported(home, monkeypatch):
    monkeypatch.chdir(home / "genesis")
    _clear()
    _write(home / ".genesis")
    assert any(row[1:] == ("~/.genesis", "write", str(home / ".genesis/owned"))
               for row in audit._S.hits)


def test_cached_native_alias_stays_conservative(home, monkeypatch):
    monkeypatch.chdir(home / "genesis")
    native = getattr(os.open, "__wrapped__", os.open)
    allowed = home.parent / "allowed"
    dfd = native(allowed, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _clear()
        fd = native("owned", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600, dir_fd=dfd)
        os.close(fd)
        assert any(row[1].startswith("relative os.open") for row in audit._S.hits)
    finally:
        os.close(dfd)


def test_known_credential_alias_write_is_not_erased(home, monkeypatch):
    secret = home / "genesis/secrets.env"
    secret.write_bytes(b"owned synthetic bytes")
    allowed = home.parent / "allowed"
    os.link(secret, allowed / "alias")
    monkeypatch.chdir(home / "genesis")
    dfd = os.open(allowed, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _clear()
        fd = os.open("alias", os.O_WRONLY | os.O_NOFOLLOW, dir_fd=dfd)
        os.close(fd)
        assert any(row[1:] == ("credential file write", "write", str(allowed / "alias"))
                   for row in audit._S.hits)
    finally:
        os.close(dfd)


def test_successful_read_with_missing_credential_metadata_keeps_fault(home):
    secret = home / "genesis/secrets.env"
    secret.write_bytes(b"owned synthetic bytes")
    alias = home.parent / "allowed/alias"
    os.link(secret, alias)
    secret.parent.chmod(0)
    try:
        _clear()
        assert alias.read_bytes() == b"owned synthetic bytes"
        assert audit._S.faults == 1
        assert any(row[1] == "observer fault" for row in audit._S.hits)
    finally:
        secret.parent.chmod(0o755)


@pytest.mark.parametrize("manual", [False, True])
def test_extra_audit_event_prevents_guess_retirement(home, monkeypatch, manual):
    allowed = home.parent / "allowed"
    (allowed / "ordinary").write_bytes(b"owned")
    monkeypatch.chdir(home / "genesis")
    enabled = True
    entered = False

    def nested(event, args):
        nonlocal entered
        if not enabled or entered or event != "open" or args[0] != "owned":
            return
        entered = True
        if manual:
            sys.audit("open", "owned", None, os.O_WRONLY)
        else:
            assert (allowed / "ordinary").read_bytes() == b"owned"

    sys.addaudithook(nested)
    try:
        _clear()
        _write(allowed)
        assert entered
        assert any(row[1].startswith("relative os.open") for row in audit._S.hits)
    finally:
        enabled = False


def test_parent_rename_keeps_original_uncertainty(home, monkeypatch):
    allowed = home.parent / "allowed"
    source = allowed / "before"
    source.mkdir()
    destination = allowed / "after"
    monkeypatch.chdir(home / "genesis")
    enabled = True

    def move(event, args):
        if enabled and event == "open" and args[0] == "owned":
            source.rename(destination)

    sys.addaudithook(move)
    try:
        _clear()
        _write(source)
        assert (destination / "owned").read_bytes() == b"owned synthetic bytes"
        assert any(row[1].startswith("relative os.open") for row in audit._S.hits)
    finally:
        enabled = False


def test_pickle_signature_and_dirfd_metadata(home):
    native = getattr(os.open, "__wrapped__", os.open)
    assert str(inspect.signature(os.open)) == str(inspect.signature(native))
    assert pickle.loads(pickle.dumps(os.open)) is os.open  # noqa: S301 -- owned builtin reference
    assert os.open in os.supports_dir_fd and native in os.supports_dir_fd


def test_invalid_descriptor_keeps_native_exception(home):
    native = getattr(os.open, "__wrapped__", os.open)
    dfd = native(home.parent / "allowed", os.O_RDONLY | os.O_DIRECTORY)
    os.close(dfd)
    results = []
    for function in [native, os.open]:
        with pytest.raises(OSError) as caught:
            function("owned", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dfd)
        results.append((type(caught.value), caught.value.errno, caught.value.filename))
    assert results[0] == results[1]


@pytest.mark.parametrize("where", ["before-event", "after-event"])
def test_snapshot_retains_pending_calls_before_they_can_finish(home, monkeypatch, where):
    witness = audit._W
    entered, finish = threading.Event(), threading.Event()
    errors = []
    original = witness._begin
    enabled = True

    def begin(*args):
        scope = original(*args)
        if where == "before-event" and scope is not None:
            entered.set()
            assert finish.wait(10)
        return scope

    def block(event, args):
        if enabled and where == "after-event" and event == "open" and args[0] == "owned":
            entered.set()
            assert finish.wait(10)

    monkeypatch.setattr(witness, "_begin", begin)
    sys.addaudithook(block)
    monkeypatch.chdir(home / "genesis")

    def run():
        try:
            _write(home.parent / "allowed")
        except BaseException as error:
            errors.append(error)

    _clear()
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert entered.wait(10)
        hits, state = witness.capture(audit._live_writes_report.census_state)
        assert state["faults"] >= 1
        assert any(row[1] == "observer fault" for row in hits)
    finally:
        enabled = False
        finish.set()
        thread.join(10)
    assert not thread.is_alive() and not errors
    assert audit._S.faults >= 1  # completion never retracts snapshot uncertainty


@pytest.mark.parametrize("metadata_in_flight", [False, True])
@pytest.mark.parametrize("protected", [False, True])
def test_snapshot_barrier_covers_new_and_metadata_in_flight_calls(home, monkeypatch, metadata_in_flight, protected):
    witness = audit._W
    metadata, proceed, emitted, finish = [threading.Event() for _ in range(4)]
    errors = []
    enabled = True
    original_parent, original_publish = witness._parent, witness._publish_pending

    def parent(*args):
        value = original_parent(*args)
        if metadata_in_flight and threading.current_thread() is thread:
            metadata.set()
            assert proceed.wait(10)
        return value

    def block(event, args):
        if enabled and event == "open" and args[0] == "owned":
            emitted.set()
            assert finish.wait(10)

    def publish(scopes):
        original_publish(scopes)
        if metadata_in_flight:
            proceed.set()
        else:
            thread.start()
        assert emitted.wait(10)

    def run():
        try:
            _write(home / ".genesis" if protected else home.parent / "allowed")
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    monkeypatch.chdir(home.parent / "allowed" if protected else home / "genesis")
    monkeypatch.setattr(witness, "_parent", parent)
    monkeypatch.setattr(witness, "_publish_pending", publish)
    sys.addaudithook(block)
    _clear()
    if metadata_in_flight:
        thread.start()
        assert metadata.wait(10)
    try:
        hits, state = witness.capture(audit._live_writes_report.census_state)
        assert state["faults"] or any(row[1].startswith("relative os.open") for row in hits)
    finally:
        enabled = False
        proceed.set()
        finish.set()
        thread.join(10)
    assert not thread.is_alive() and not errors


def test_snapshot_exception_releases_barrier_and_later_calls_refine(home, monkeypatch):
    def broken(state):
        raise RuntimeError("owned snapshot failure")

    with pytest.raises(RuntimeError, match="owned snapshot failure"):
        audit._W.capture(broken)
    monkeypatch.chdir(home / "genesis")
    _clear()
    _write(home.parent / "allowed")
    assert not audit._S.hits and audit._S.faults == 0


@pytest.mark.parametrize("flags", [os.O_WRONLY, os.O_RDONLY | os.O_CREAT,
                                  os.O_WRONLY | getattr(os, "O_PATH", 0)])
def test_unsupported_flags_keep_original_observations(home, monkeypatch, flags):
    monkeypatch.chdir(home / "genesis")
    allowed = home.parent / "allowed"
    (allowed / "owned").write_bytes(b"owned")
    dfd = os.open(allowed, os.O_RDONLY | os.O_DIRECTORY)
    try:
        observed = []
        for function in [getattr(os.open, "__wrapped__", os.open), os.open]:
            _clear()
            fd = function("owned", flags, dir_fd=dfd)
            os.close(fd)
            observed.append((dict(audit._S.hits), audit._S.faults))
        assert observed[0] == observed[1]
    finally:
        os.close(dfd)


def test_native_failure_keeps_attempt_and_exception(home, monkeypatch):
    monkeypatch.chdir(home / "genesis")
    allowed = home.parent / "allowed"
    (allowed / "owned").write_bytes(b"owned")
    dfd = os.open(allowed, os.O_RDONLY | os.O_DIRECTORY)
    try:
        _clear()
        with pytest.raises(FileExistsError):
            os.open("owned", os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=dfd)
        assert any(row[1].startswith("relative os.open") for row in audit._S.hits)
        assert (allowed / "owned").read_bytes() == b"owned"
    finally:
        os.close(dfd)


@pytest.mark.parametrize("leaf", ["unicodé", b"raw-\xff", "ordinary"])
def test_exact_native_path_forms_resolve_to_the_actual_destination(home, monkeypatch, leaf):
    monkeypatch.chdir(home / "genesis")
    allowed = home.parent / "allowed"
    _clear()
    _write(allowed, leaf)
    assert (allowed / os.fsdecode(leaf)).read_bytes() == b"owned synthetic bytes"
    assert not audit._S.hits and audit._S.faults == 0


@pytest.mark.parametrize("signal", [KeyboardInterrupt, SystemExit, GeneratorExit])
@pytest.mark.parametrize("where", ["judgment", "completion"])
def test_control_after_native_success_closes_the_owned_descriptor(home, monkeypatch, signal, where):
    witness = audit._W
    closed = []
    original = witness._close

    def close(fd):
        closed.append(fd)
        original(fd)

    def interrupted(*args):
        raise signal()

    monkeypatch.chdir(home / "genesis")
    monkeypatch.setattr(witness, "_close", close)
    if where == "judgment":
        monkeypatch.setattr(witness, "_resolved", interrupted)
    else:
        monkeypatch.setattr(witness, "_retain", interrupted)
        monkeypatch.setattr(witness, "_resolved", lambda *args: False)
    dfd = os.open(home.parent / "allowed", os.O_RDONLY | os.O_DIRECTORY)
    try:
        _clear()
        with pytest.raises(signal):
            os.open("owned", os.O_WRONLY | os.O_CREAT | os.O_EXCL, dir_fd=dfd)
        assert len(closed) == 1
        with pytest.raises(OSError):
            os.fstat(closed[0])
    finally:
        os.close(dfd)


@pytest.mark.parametrize("stage", ["observation", "post-metadata", "deferral"])
def test_witness_failure_preserves_native_success_and_primary_evidence(home, monkeypatch, stage):
    witness = audit._W
    original_parent = witness._parent
    calls = 0

    def broken(*args):
        raise RuntimeError("PRIVATE_WITNESS_MESSAGE")

    def parent(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("PRIVATE_WITNESS_MESSAGE")
        return original_parent(*args)

    attribute, replacement = {"observation": ("observe", broken), "deferral": ("defer", broken),
                              "post-metadata": ("_parent", parent)}[stage]
    monkeypatch.chdir(home / "genesis")
    monkeypatch.setattr(witness, attribute, replacement)
    _clear()
    _write(home.parent / "allowed")
    assert (home.parent / "allowed/owned").read_bytes() == b"owned synthetic bytes"
    assert audit._S.faults >= 1
    assert any(row[1].startswith("relative os.open") for row in audit._S.hits)
    assert "PRIVATE_WITNESS" not in repr(audit._S.hits)


def test_unresolved_completion_remains_registered_until_rows_are_published(home, monkeypatch):
    witness = audit._W
    pending, finish = threading.Event(), threading.Event()
    original = witness._retain
    errors = []
    allowed = home.parent / "allowed"
    (allowed / "owned").write_bytes(b"owned")

    def retain(scope, **kwargs):
        if threading.current_thread() is thread:
            pending.set()
            assert finish.wait(10)
        return original(scope, **kwargs)

    def run():
        try:
            with pytest.raises(FileExistsError):
                _write(allowed)
        except BaseException as error:
            errors.append(error)

    monkeypatch.setattr(witness, "_retain", retain)
    monkeypatch.chdir(home / "genesis")
    thread = threading.Thread(target=run)
    _clear()
    thread.start()
    try:
        assert pending.wait(10)
        hits, state = witness.capture(audit._live_writes_report.census_state)
        assert state["faults"] >= 1
        assert any(row[1].startswith("relative os.open") for row in hits)
    finally:
        finish.set()
        thread.join(10)
    assert not thread.is_alive() and not errors


def test_wrapper_cleanup_is_owned_and_installation_is_independently_idempotent(home, monkeypatch):
    witness = audit._W
    native, wrapped = witness.native, os.open
    witness.install()
    assert os.open is wrapped

    def other(*args, **kwargs):
        return native(*args, **kwargs)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "open", other)
            witness.close()
            witness.close()
            assert os.open is other and wrapped not in os.supports_dir_fd
            witness.install()
            assert os.open is other and not witness.installed
    finally:
        # The context restores our now-retired wrapper; restore the native owner
        # before reinstalling, as a different plugin must never be wrapped.
        os.open = native
        witness.install()
    assert witness.installed and os.open in os.supports_dir_fd and native in os.supports_dir_fd


@pytest.mark.parametrize("case", range(7))
def test_forwarded_native_errors_preserve_class_message_errno_and_filename(home, case):
    native = audit._W.native
    cases = [((), {}), (("x",), {}), (("x", 0, 0, 0), {}), (("x", 0), {"unexpected": True}),
             (("x", object()), {}), ((home / "missing", os.O_RDONLY), {}),
             ((b"bad\x00name", os.O_RDONLY), {})]
    args, kwargs = cases[case]
    observed = []
    for function in [native, os.open]:
        with pytest.raises((TypeError, ValueError, OSError)) as caught:
            function(*args, **kwargs)
        error = caught.value
        observed.append((type(error), str(error), getattr(error, "errno", None),
                         getattr(error, "filename", None)))
    assert observed[0] == observed[1]


def test_successful_correction_never_subtracts_existing_faults_or_published_rows(home, monkeypatch):
    monkeypatch.chdir(home / "genesis")
    _clear()
    for _ in range(3):
        audit._unknown("open", "judge", PermissionError("owned private message"))
    before = dict(audit._S.hits)
    _write(home.parent / "allowed")
    assert audit._S.faults == 3 and dict(audit._S.hits) == before


def test_install_failure_keeps_primary_observation_and_owned_cleanup(home, monkeypatch):
    witness = audit._W

    def broken(*args):
        raise RuntimeError("PRIVATE_INSTALL_FAILURE")

    witness.close()
    try:
        _clear()
        with monkeypatch.context() as patch:
            patch.setattr(inspect, "signature", broken)
            witness.install()
        assert not witness.installed and os.open is witness.native
        assert audit._S.installed and audit._S.faults == 1
        target = home / ".genesis/still-observed"
        target.mkdir()
        assert any(row[2:] == ("os.mkdir", str(target)) for row in audit._S.hits)
        assert "PRIVATE_INSTALL" not in repr(audit._S.hits)
    finally:
        witness.install()
    assert witness.installed and os.open in os.supports_dir_fd


@pytest.mark.parametrize("protected", [False, True])
def test_concurrent_native_calls_keep_their_own_destinations(home, monkeypatch, protected):
    allowed = home.parent / "allowed"
    destination = home / ".genesis" if protected else allowed
    monkeypatch.chdir(allowed if protected else home / "genesis")
    _clear()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda index: _write(destination, f"owned-{index}"), range(12)))
    assert all((destination / f"owned-{index}").read_bytes() == b"owned synthetic bytes" for index in range(12))
    assert audit._S.faults == 0
    rows = [row for row in audit._S.hits if row[2] == "write"]
    assert len(rows) == (12 if protected else 0)
    assert all(row[1] == "~/.genesis" for row in rows)
