"""Current-key capture authority covers live, vanished and legacy archives."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_scripts.test_transcript_archive import archive


@pytest.fixture
def capture_set(tmp_path, monkeypatch):
    root, destination, scratch, gpg = [
        tmp_path / name for name in ("root", "destination", "scratch", "gpg")
    ]
    for path in (root, destination, scratch, gpg):
        path.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(gpg))
    (root / "a.jsonl").write_text("{}\n")
    return root, destination, scratch


def test_real_rotation_preserves_unreadable_inventory_and_captures(capture_set):
    root, destination, scratch = capture_set
    password = b"old-test-passphrase"
    assert not archive.backup(root, destination, scratch, password)
    target = destination / archive.object_name("a.jsonl")
    index = destination / archive._INDEX_NAME
    old, inventory = target.read_bytes(), index.read_bytes()
    for vanished in (False, True):
        if vanished:
            (root / "a.jsonl").unlink()
        with pytest.raises(ValueError, match="retained capture history"):
            archive.backup(root, destination, scratch, b"new-test-passphrase")
        with pytest.raises(ValueError, match="retained capture history"):
            archive.pool_manifest(destination, scratch / "manifest", "snapshot", scratch,
                                  b"new-test-passphrase")
        assert target.read_bytes() == old
        assert index.read_bytes() == inventory
        assert not (scratch / "manifest").exists()
        assert not archive.backup(root, destination, scratch, password)


@pytest.mark.parametrize("state", ["missing", "corrupt", "plaintext-forged"])
def test_index_cannot_authorize_without_current_key(capture_set, state):
    root, destination, scratch = capture_set
    password = b"old-test-passphrase"
    assert not archive.backup(root, destination, scratch, password)
    target = destination / archive.object_name("a.jsonl")
    (root / "a.jsonl").unlink()
    index = destination / archive._INDEX_NAME
    if state == "corrupt":
        index.write_bytes(b"not an encrypted index")
    else:
        index.unlink()
    if state == "plaintext-forged":
        (destination / ".capture-index.json").write_text(
            json.dumps({"a.jsonl": {"source": [0] * 5, "ciphertext": archive.checksum(target)}})
        )
    if state == "corrupt":
        before = index.read_bytes()
        for key in (b"wrong-test-passphrase", password):
            with pytest.raises(ValueError, match="retained capture history"):
                archive.backup(root, destination, scratch, key)
            assert index.read_bytes() == before
    else:
        assert archive.backup(root, destination, scratch, b"wrong-test-passphrase")
        # The failed attempt published inventory encrypted under its own key.
        # An unreadable retained inventory must never be silently replaced.
        retained = index.read_bytes()
        ciphertext = target.read_bytes()
        with pytest.raises(ValueError, match="retained capture history"):
            archive.backup(root, destination, scratch, password)
        assert index.read_bytes() == retained and target.read_bytes() == ciphertext
        # Explicit retirement in this synthetic fixture permits re-enrollment.
        index.unlink()
        assert not archive.backup(root, destination, scratch, password)
        document, authenticated = archive.capture_index(destination, scratch, password)
        assert authenticated and archive.object_name("a.jsonl") in document["captures"]
        assert target.read_bytes() == ciphertext
    assert target.exists()


def test_missing_index_enrolls_current_key_vanished_and_legacy(capture_set):
    root, destination, scratch = capture_set
    password = b"test-passphrase"
    assert not archive.backup(root, destination, scratch, password)
    (root / "a.jsonl").unlink()
    (destination / archive._INDEX_NAME).unlink()
    plain = scratch / "legacy.jsonl"
    plain.write_text('{"legacy":true}\n')
    archive.crypt(plain, destination / "legacy.jsonl.gpg", password)
    assert not archive.backup(root, destination, scratch, password)
    document, authenticated = archive.capture_index(destination, scratch, password)
    assert authenticated
    assert set(document["captures"]) == {archive.object_name("a.jsonl"), "legacy.jsonl.gpg"}
    rows = archive.pool_manifest(destination, scratch / "manifest", "snapshot", scratch, password)
    assert len(rows) == 2
    assert all(row[0] != archive._INDEX_NAME for row in rows)


@pytest.mark.parametrize("state", ["wrong-key", "corrupt", "malformed", "symlink", "directory"])
@pytest.mark.parametrize("operation", ["index", "backup", "manifest"])
def test_existing_unreadable_history_preserved_for_every_entrypoint(capture_set, state, operation):
    root, destination, scratch = capture_set
    password = b"history-test-passphrase"
    index = destination / archive._INDEX_NAME
    document = archive._empty_index()
    missing = archive.object_name("vanished.jsonl")
    document["captures"][missing] = "a" * 64
    archive.publish_capture_index(destination, scratch, password, document)
    key = password
    if state == "wrong-key":
        key = b"different-history-passphrase"
    elif state == "corrupt":
        index.write_bytes(b"not encrypted")
    elif state == "malformed":
        plain = scratch / "malformed.json"
        plain.write_text('{"version":1,"sources":{},"captures":null}')
        archive.crypt(plain, index, password)
    elif state == "symlink":
        retained = destination / "retained-index"
        index.rename(retained)
        index.symlink_to(retained.name)
    elif state == "directory":
        index.unlink()
        index.mkdir()
    before = index.read_bytes() if state != "directory" else None
    actions = {
        "index": lambda: archive.capture_index(destination, scratch, key),
        "backup": lambda: archive.backup(root, destination, scratch, key),
        "manifest": lambda: archive.pool_manifest(
            destination, scratch / "manifest", "snapshot", scratch, key
        ),
    }
    with pytest.raises(ValueError, match="retained capture history"):
        actions[operation]()
    if before is not None:
        assert index.read_bytes() == before
    else:
        assert index.is_dir() and not list(index.iterdir())
    assert index.is_symlink() == (state == "symlink")
    assert not (scratch / "manifest").exists()
    assert not list(destination.glob("*.gpg"))


def test_recovered_history_retains_missing_object_and_stays_incomplete(capture_set):
    root, destination, scratch = capture_set
    password = b"recoverable-history-passphrase"
    document = archive._empty_index()
    missing = archive.object_name("vanished.jsonl")
    document["captures"][missing] = "a" * 64
    archive.publish_capture_index(destination, scratch, password, document)
    assert archive.backup(root, destination, scratch, password)
    retained, authenticated = archive.capture_index(destination, scratch, password)
    assert authenticated and retained["captures"][missing] == "a" * 64
    with pytest.raises(ValueError, match="retained capture missing"):
        archive.pool_manifest(destination, scratch / "manifest", "snapshot", scratch, password)
    assert not (scratch / "manifest").exists()


def test_index_publication_interruption_retains_artifact(capture_set, monkeypatch):
    root, destination, scratch = capture_set
    actual = archive.os.replace

    def interrupted(source, target):
        if Path(target).name == archive._INDEX_NAME:
            raise OSError("simulated interrupted index publication")
        return actual(source, target)

    monkeypatch.setattr(archive.os, "replace", interrupted)
    password = b"test-passphrase"
    assert archive.backup(root, destination, scratch, password)
    target = destination / archive.object_name("a.jsonl")
    assert target.is_file()
    assert not (destination / archive._INDEX_NAME).exists()
    monkeypatch.setattr(archive.os, "replace", actual)
    assert not archive.backup(root, destination, scratch, password)
    assert not list(destination.glob(".index-*"))
    assert not list(scratch.iterdir())


def test_all_crypto_disables_symmetric_cache(capture_set, monkeypatch):
    if not shutil.which("gpg"):
        pytest.skip("gpg not available")
    root, destination, _ = capture_set
    source, encrypted, restored = root / "source", destination / "payload.gpg", root / "restored"
    source.write_bytes(b"synthetic cache-control payload")
    actual = subprocess.run
    commands = []

    def invoked(args, **kwargs):
        commands.append(args)
        return actual(args, **kwargs)

    monkeypatch.setattr(archive.subprocess, "run", invoked)
    try:
        archive.crypt(source, encrypted, b"test")
        archive.crypt(encrypted, restored, b"test", decrypt=True)
        assert restored.read_bytes() == source.read_bytes()
        assert len(commands) == 2
        assert all("--no-symkey-cache" in command for command in commands)
    finally:
        actual(["gpgconf", "--homedir", str(root.parent / "gpg"), "--kill", "gpg-agent"],
               capture_output=True, check=False)




def test_manifest_rejects_capture_changed_after_attestation(capture_set, monkeypatch):
    root, destination, scratch = capture_set
    password = b"test-passphrase"
    assert not archive.backup(root, destination, scratch, password)
    target = destination / archive.object_name("a.jsonl")
    actual = archive.attest_captures

    def mutate_after(*args):
        result = actual(*args)
        target.write_bytes(target.read_bytes() + b"changed")
        return result

    monkeypatch.setattr(archive, "attest_captures", mutate_after)
    with pytest.raises(ValueError, match="changed after authentication"):
        archive.pool_manifest(destination, scratch / "manifest", "snapshot", scratch, password)
    assert not (scratch / "manifest").exists()


def test_authenticated_index_avoids_per_archive_decryption(capture_set, monkeypatch):
    root, destination, scratch = capture_set
    password = b"test-passphrase"
    assert not archive.backup(root, destination, scratch, password)
    (root / "a.jsonl").unlink()
    calls = []
    actual = archive.crypt

    def count(source, target, *args, **kwargs):
        if kwargs.get("decrypt"):
            calls.append(source.name)
        return actual(source, target, *args, **kwargs)

    monkeypatch.setattr(archive, "crypt", count)
    assert not archive.backup(root, destination, scratch, password)
    assert calls == [archive._INDEX_NAME]


def test_rotation_preserves_scope_fence_and_reports_retained_other_project(capture_set):
    root, destination, scratch = capture_set
    for project in ("main", "other"):
        (root / project).mkdir()
        (root / project / "a.jsonl").write_text("{}\n")
    assert not archive.backup(root, destination, scratch, b"old-test-passphrase")
    other = destination / archive.object_name("other/a.jsonl")
    before = other.read_bytes()
    inventory = (destination / archive._INDEX_NAME).read_bytes()
    for project in ("main", None):
        with pytest.raises(ValueError, match="retained capture history"):
            archive.backup(root, destination, scratch, b"new-test-passphrase", project=project)
        assert other.read_bytes() == before
        assert (destination / archive._INDEX_NAME).read_bytes() == inventory
    assert not archive.backup(root, destination, scratch, b"old-test-passphrase", project="main")


def test_checkpoint_prefix_survives_interruption_and_rotation(capture_set, monkeypatch):
    root, destination, scratch = capture_set
    (root / "b.jsonl").write_text("{}\n")
    password = b"checkpoint-test-passphrase"
    monkeypatch.setattr(archive, "_CHECKPOINT_OBJECTS", 1)
    actual = archive.publish_capture_index
    publications = []

    def interrupt(*args):
        actual(*args)
        publications.append(1)
        raise KeyboardInterrupt

    monkeypatch.setattr(archive, "publish_capture_index", interrupt)
    with pytest.raises(KeyboardInterrupt):
        archive.backup(root, destination, scratch, password)
    document, authenticated = archive.capture_index(destination, scratch, password)
    assert authenticated and len(document["sources"]) == 1
    relative = next(iter(document["sources"]))
    target = destination / archive.object_name(relative)
    prefix = target.read_bytes()
    monkeypatch.setattr(archive, "publish_capture_index", actual)
    assert not archive.backup(root, destination, scratch, password)
    assert target.read_bytes() == prefix
    assert len(publications) == 1

    monkeypatch.setattr(archive, "publish_capture_index", interrupt)
    inventory = (destination / archive._INDEX_NAME).read_bytes()
    with pytest.raises(ValueError, match="retained capture history"):
        archive.backup(root, destination, scratch, b"rotated-checkpoint-test-passphrase")
    assert len(publications) == 1
    assert (destination / archive._INDEX_NAME).read_bytes() == inventory
    assert target.read_bytes() == prefix


def test_fatal_checkpoint_does_not_continue_or_retry(capture_set, monkeypatch):
    root, destination, scratch = capture_set
    (root / "b.jsonl").write_text("{}\n")
    monkeypatch.setattr(archive, "_CHECKPOINT_OBJECTS", 1)
    calls = []

    def fail(*args):
        calls.append("publication")
        raise OSError("disk unavailable")

    def forbidden(*args):
        pytest.fail("fatal checkpoint must stop before final attestation")

    monkeypatch.setattr(archive, "publish_capture_index", fail)
    monkeypatch.setattr(archive, "attest_captures", forbidden)
    assert archive.backup(root, destination, scratch, b"checkpoint-test-passphrase")
    assert calls == ["publication"]
    assert len(list(destination.glob("*.gpg"))) == 1


def test_pool_incomplete_inventory_preserves_successful_enrollment(capture_set):
    root, destination, scratch = capture_set
    password = b"checkpoint-test-passphrase"
    assert not archive.backup(root, destination, scratch, password)
    target = destination / archive.object_name("a.jsonl")
    (destination / archive._INDEX_NAME).unlink()
    (destination / "bad.jsonl.gpg").write_bytes(b"invalid encrypted capture")
    with pytest.raises(ValueError, match="not authenticated"):
        archive.pool_manifest(destination, scratch / "manifest", "snapshot", scratch, password)
    document, authenticated = archive.capture_index(destination, scratch, password)
    assert authenticated and document["captures"] == {target.name: archive.checksum(target)}
    assert not (scratch / "manifest").exists()


def test_checkpoint_elapsed_and_count_boundaries(monkeypatch, tmp_path):
    from types import SimpleNamespace

    clock = [0]
    publications = []
    monkeypatch.setattr(archive, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(
        archive, "publish_capture_index", lambda *args: publications.append(clock[0])
    )
    monkeypatch.setattr(archive, "_CHECKPOINT_OBJECTS", 2)
    checkpoint = archive.CaptureCheckpoint(tmp_path, tmp_path, b"key", {}, True)
    checkpoint.changed()
    assert publications == [0] and checkpoint.first_authorization_published
    checkpoint.changed()
    assert publications == [0] and checkpoint.pending == 1
    checkpoint.changed()
    assert publications == [0, 0] and checkpoint.pending == 0
    clock[0] = 60
    checkpoint.changed(authorization=False)
    assert publications == [0, 0, 60] and not checkpoint.dirty
    checkpoint.flush()
    assert publications == [0, 0, 60]


@pytest.mark.parametrize("mode", ["capture", "enrollment"])
def test_process_kill_reuses_published_prefix(capture_set, monkeypatch, mode):
    import os
    import subprocess
    import sys

    root, destination, scratch = capture_set
    (root / "b.jsonl").write_text("{}\n")
    password = b"killed-checkpoint-test-passphrase"
    if mode == "enrollment":
        assert not archive.backup(root, destination, scratch, password)
        (destination / archive._INDEX_NAME).unlink()
        for source in root.iterdir():
            source.unlink()
    script = """import importlib.util, os, signal, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location('checkpoint_archive', sys.argv[1])
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
a._CHECKPOINT_OBJECTS = 1
publish = a.publish_capture_index
def kill(*args):
    publish(*args)
    os.kill(os.getpid(), signal.SIGKILL)
a.publish_capture_index = kill
a.backup(*(Path(value) for value in sys.argv[2:5]), b'killed-checkpoint-test-passphrase')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, archive.__file__, str(root), str(destination), str(scratch)],
        env=os.environ.copy(),
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == -9, result.stderr
    document, authenticated = archive.capture_index(destination, scratch, password)
    assert authenticated and len(document["captures"]) == 1
    name = next(iter(document["captures"]))
    prefix = (destination / name).read_bytes()
    calls = []
    actual = archive.crypt

    def observed(source, target, *args, **kwargs):
        calls.append((source.name, kwargs.get("decrypt", False)))
        return actual(source, target, *args, **kwargs)

    monkeypatch.setattr(archive, "crypt", observed)
    assert not archive.backup(root, destination, scratch, password)
    assert (destination / name).read_bytes() == prefix
    assert (name, True) not in calls
    assert len([call for call in calls if call == ("source.tar", False)]) == (
        0 if mode == "enrollment" else 1
    )


@pytest.mark.parametrize("failure", ["artifact-directory", "index-rename", "index-directory"])
def test_durability_failures_never_authorize_missing_ciphertext(capture_set, monkeypatch, failure):
    root, destination, scratch = capture_set
    password = b"durability-test-passphrase"
    actual_sync, actual_replace = archive._fsync_directory, archive.os.replace
    events = []

    def replace(source, target):
        name = Path(target).name
        events.append(("rename", name))
        if failure == "index-rename" and name == archive._INDEX_NAME:
            raise OSError("index rename failure")
        return actual_replace(source, target)

    def sync(directory):
        events.append(("sync", Path(directory).name))
        artifact_published = ("rename", archive.object_name("a.jsonl")) in events
        index_published = any(event == ("rename", archive._INDEX_NAME) for event in events)
        if Path(directory) == destination and (
            (failure == "artifact-directory" and artifact_published and not index_published)
            or (failure == "index-directory" and index_published)
        ):
            raise OSError("directory sync failure")
        return actual_sync(directory)

    monkeypatch.setattr(archive.os, "replace", replace)
    monkeypatch.setattr(archive, "_fsync_directory", sync)
    if failure == "artifact-directory":
        with pytest.raises(OSError, match="directory sync failure"):
            archive.backup(root, destination, scratch, password)
    else:
        assert archive.backup(root, destination, scratch, password)
    if failure == "index-directory":
        with pytest.raises(OSError, match="directory sync failure"):
            archive.capture_index(destination, scratch, password)
    else:
        _, authenticated = archive.capture_index(destination, scratch, password)
        assert not authenticated
    artifact_rename = ("rename", archive.object_name("a.jsonl"))
    assert artifact_rename in events
    if ("rename", archive._INDEX_NAME) in events:
        publication_events = events[events.index(artifact_rename):]
        assert (
            publication_events.index(artifact_rename)
            < publication_events.index(("sync", destination.name))
            < publication_events.index(("rename", archive._INDEX_NAME))
        )
    monkeypatch.setattr(archive.os, "replace", actual_replace)
    monkeypatch.setattr(archive, "_fsync_directory", actual_sync)
    assert not archive.backup(root, destination, scratch, password)
    document, authenticated = archive.capture_index(destination, scratch, password)
    assert authenticated
    for name, digest in document["captures"].items():
        assert (destination / name).is_file()
        assert archive.checksum(destination / name) == digest


def test_fatal_enrollment_checkpoint_stops_before_next_capture(capture_set, monkeypatch):
    root, destination, scratch = capture_set
    (root / "b.jsonl").write_text("{}\n")
    password = b"enrollment-stop-test-passphrase"
    assert not archive.backup(root, destination, scratch, password)
    (destination / archive._INDEX_NAME).unlink()
    monkeypatch.setattr(archive, "_CHECKPOINT_OBJECTS", 1)
    actual = archive.crypt
    decrypted, published = [], []

    def observe(source, target, *args, **kwargs):
        if kwargs.get("decrypt"):
            decrypted.append(source.name)
        return actual(source, target, *args, **kwargs)

    def fail(*args):
        published.append(1)
        raise OSError("checkpoint stage failed")

    monkeypatch.setattr(archive, "crypt", observe)
    monkeypatch.setattr(archive, "publish_capture_index", fail)
    with pytest.raises(archive.CaptureCheckpointError):
        archive.pool_manifest(destination, scratch / "manifest", "snapshot", scratch, password)
    assert len(decrypted) == 1 and published == [1]
    assert not (scratch / "manifest").exists()
    assert len(list(destination.glob("*.gpg"))) == 2


def test_first_authorization_not_consumed_by_invalidation_or_failed_publication(
    monkeypatch, tmp_path
):
    from types import SimpleNamespace

    clock = [0]
    publications = []
    monkeypatch.setattr(archive, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def publish(*args):
        publications.append(1)
        if len(publications) == 2:
            raise OSError("publication did not finish")

    monkeypatch.setattr(archive, "publish_capture_index", publish)
    checkpoint = archive.CaptureCheckpoint(tmp_path, tmp_path, b"key", {}, True)
    checkpoint.changed(authorization=False)
    assert not publications and not checkpoint.first_authorization_published
    clock[0] = 60
    checkpoint.changed(authorization=False)
    assert publications == [1] and not checkpoint.first_authorization_published
    with pytest.raises(archive.CaptureCheckpointError):
        checkpoint.changed()
    assert not checkpoint.first_authorization_published and checkpoint.dirty
    checkpoint.changed()
    assert publications == [1, 1, 1] and checkpoint.first_authorization_published
    assert not checkpoint.dirty and checkpoint.pending == 0


@pytest.mark.parametrize("mode", ["capture", "enrollment"])
def test_repeated_termination_after_first_new_authorization_keeps_progress(capture_set, mode):
    import os
    import subprocess
    import sys

    root, destination, scratch = capture_set
    for name in ("b.jsonl", "c.jsonl"):
        (root / name).write_text('{"before":true}\n')
    password = b"first-authorization-test-passphrase"
    assert not archive.backup(root, destination, scratch, password)
    document, authenticated = archive.capture_index(destination, scratch, password)
    assert authenticated
    if mode == "capture":
        for name in ("a.jsonl", "b.jsonl"):
            (root / name).write_text('{"changed":true}\n')
        first = archive.object_name("a.jsonl")
        blocked = "b.jsonl"
    else:
        names = sorted(document["captures"])
        first, blocked = names[:2]
        document["captures"] = {names[2]: document["captures"][names[2]]}
        archive.publish_capture_index(destination, scratch, password, document)
    counter = scratch / "first-work-count"
    script = """import importlib.util, os, signal, sys, tarfile
from pathlib import Path
spec = importlib.util.spec_from_file_location('first_archive', sys.argv[1])
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)
root, destination, scratch = map(Path, sys.argv[2:5])
mode, first, blocked = sys.argv[5:8]
crypt = a.crypt
counter = scratch / 'first-work-count'
def observed(source, target, password, decrypt=False):
    selected = source.name
    if mode == 'capture' and not decrypt and source.name == 'source.tar':
        with tarfile.open(source) as incoming:
            selected = incoming.getnames()[0]
    if (mode == 'capture' and selected == blocked and not decrypt) or (
        mode == 'enrollment' and selected == blocked and decrypt
    ):
        # Model an object that consumes the remaining invocation window:
        # terminate before it can finish or trigger a later checkpoint.
        os.kill(os.getpid(), signal.SIGTERM)
    if (mode == 'capture' and selected == 'a.jsonl' and not decrypt) or (
        mode == 'enrollment' and selected == first and decrypt
    ):
        counter.write_text(str(int(counter.read_text()) + 1 if counter.exists() else 1))
    return crypt(source, target, password, decrypt)
a.crypt = observed
signal.signal(signal.SIGTERM, lambda number, frame: sys.exit(128 + number))
password = b'first-authorization-test-passphrase'
if mode == 'capture':
    a.backup(root, destination, scratch, password)
else:
    a.pool_manifest(destination, scratch / 'manifest', 'snapshot', scratch, password)
"""
    previous = None
    for _ in range(2):
        result = subprocess.run(
            [sys.executable, "-c", script, archive.__file__, str(root), str(destination),
             str(scratch), mode, first, blocked],
            env=os.environ.copy(), capture_output=True, timeout=30,
        )
        assert result.returncode == 143, result.stderr
        committed, authenticated = archive.capture_index(destination, scratch, password)
        assert authenticated and first in committed["captures"]
        if mode == "capture":
            assert committed["sources"]["a.jsonl"]["source"] == archive.fingerprint(
                (root / "a.jsonl").lstat()
            )
        ciphertext = (destination / first).read_bytes()
        if previous is not None:
            assert ciphertext == previous
        previous = ciphertext
    assert counter.read_text() == "1"
    assert not (scratch / "manifest").exists()
