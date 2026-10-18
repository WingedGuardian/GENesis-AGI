"""Retained coverage and malformed archive protocol boundaries."""

import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import sysconfig
import tarfile
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "archive_review_subject", Path(__file__).parents[2] / "scripts/lib/transcript_archive.py"
)
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


def test_missing_authenticated_capture_remains_incomplete(tmp_path):
    document = archive._empty_index()
    name = archive.object_name("vanished.jsonl")
    document["captures"][name] = "a" * 64
    _, failures = archive.attest_captures(tmp_path, tmp_path, b"synthetic", document)
    assert failures
    assert name in document["captures"]


@pytest.mark.parametrize("password", [b"first\nsecond", b"first\n", b"\nsecond",
                                      b"first\0second", b"first\0", b"\0second"])
def test_passphrase_bytes_are_never_silently_truncated(tmp_path, monkeypatch, password):
    monkeypatch.setattr(archive.subprocess, "run", lambda *a, **kw: pytest.fail("GPG must not run"))
    with pytest.raises(ValueError):
        archive.crypt(tmp_path / "input", tmp_path / "output", password)


@pytest.mark.parametrize("name", [".partial-0-../../outside", ".partial-0-path/child",
                                 ".partial-0-1-2-3/child", ".partial-aborted"])
@pytest.mark.parametrize("operation", ["pool_missing", "pool_gc"])
def test_partial_object_grammar_is_flat_and_complete(tmp_path, name, operation):
    refs, objects = tmp_path / "refs", tmp_path / "objects"
    refs.write_text("")
    objects.write_text(name + "\n")
    with pytest.raises(ValueError):
        getattr(archive, operation)(refs, objects)


@pytest.mark.parametrize("name,mtime", [("x\0y", "1"), ("a.jsonl", str(10**100))])
def test_raw_dryrun_refuses_unrestorable_metadata(tmp_path, name, mtime):
    payload = tmp_path / "payload.tar"
    with tarfile.open(payload, "w", format=tarfile.PAX_FORMAT) as outgoing:
        member = tarfile.TarInfo(name)
        member.size = 1
        member.pax_headers = {"path": name, "genesis.mtime_ns": mtime,
                              "genesis.sha256": hashlib.sha256(b"x").hexdigest()}
        outgoing.addfile(member, io.BytesIO(b"x"))
    with pytest.raises(ValueError):
        archive.restore(payload, tmp_path / "target", archive.object_name(name), dry_run=True)
    assert not (tmp_path / "target").exists()


def test_safe_target_refuses_nul(tmp_path):
    with pytest.raises(ValueError):
        archive.safe_target(tmp_path, "x\0y")


@pytest.mark.parametrize("raw", [str(-(10**100)), str((2**63) * 1_000_000_000)])
def test_capture_timestamp_rejects_platform_seconds_overflow(raw):
    with pytest.raises(ValueError):
        archive._capture_timestamp(raw)


@pytest.mark.parametrize("value", [-1, 0, 1234567890000000001, 2**63 + 1])
def test_timestamp_validation_preserves_valid_nanosecond_values(value):
    assert sysconfig.get_config_var("SIZEOF_TIME_T") == 8
    assert archive._capture_timestamp(str(value)) == value


def test_missing_capture_repair_restores_coverage(tmp_path):
    name = archive.object_name("vanished.jsonl")
    path = tmp_path / name
    path.write_bytes(b"previously-authenticated-synthetic-ciphertext")
    document = archive._empty_index()
    document["captures"][name] = archive.checksum(path)
    document["validated_captures"][name] = archive.checksum(path)
    assert archive.attest_captures(tmp_path, tmp_path, b"synthetic", document) == (False, [])


def test_valid_partial_uploads_are_retained_or_expire_by_age(tmp_path):
    refs, objects = tmp_path / "refs", tmp_path / "objects"
    refs.write_text("")
    objects.write_text(".partial-0-1-2-3\n.partial-1000000-1-2-3\n")
    assert archive.pool_missing(refs, objects) == []
    assert archive.pool_gc(refs, objects, now=1000001) == [".partial-0-1-2-3"]


def test_capture_index_rejects_nul_source_name(tmp_path, monkeypatch):
    document = archive._empty_index()
    document["sources"]["x\0y"] = {"source": [1, 2, 3, 4, 5], "ciphertext": "a" * 64}
    (tmp_path / archive._INDEX_NAME).write_text(json.dumps(document))
    monkeypatch.setattr(archive, "crypt", lambda source, target, *a, **kw: shutil.copyfile(source, target))
    before = (tmp_path / archive._INDEX_NAME).read_bytes()
    with pytest.raises(ValueError, match="retained capture history"):
        archive.capture_index(tmp_path, tmp_path, b"synthetic")
    assert (tmp_path / archive._INDEX_NAME).read_bytes() == before


def test_durable_directory_fences_existing_ancestor_entries_on_retry(tmp_path, monkeypatch):
    destination = tmp_path / "new" / "nested"
    calls = []
    original = archive._fsync_directory
    def observed(path):
        calls.append(path)
        original(path)
    monkeypatch.setattr(archive, "_fsync_directory", observed)
    archive._durable_directory(destination)
    expected = [p.parent for p in (destination.resolve(), *destination.resolve().parents) if p.parent != p]
    assert calls == expected
    calls.clear()
    archive._durable_directory(destination)
    assert calls == expected


def test_raw_restore_fences_directory_after_final_replace(tmp_path, monkeypatch):
    payload = tmp_path / "payload.tar"
    name = "parent/a.jsonl"
    with tarfile.open(payload, "w", format=tarfile.PAX_FORMAT) as outgoing:
        member = tarfile.TarInfo(name)
        member.size = 1
        member.pax_headers = {"genesis.mtime_ns": "1234567890000000001",
                              "genesis.sha256": hashlib.sha256(b"x").hexdigest()}
        outgoing.addfile(member, io.BytesIO(b"x"))
    events = []
    replace, fence = os.replace, archive._fsync_directory
    def replaced(source, target):
        events.append(("replace", target))
        replace(source, target)
    def fenced(path):
        events.append(("fence", path))
        fence(path)
    monkeypatch.setattr(archive.os, "replace", replaced)
    monkeypatch.setattr(archive, "_fsync_directory", fenced)
    root = tmp_path / "restored"
    assert archive.restore(payload, root, archive.object_name(name))
    assert events[-2:] == [("replace", root / name), ("fence", (root / name).parent)]
    assert (root / name).stat().st_mtime_ns == 1234567890000000001


def test_restored_timestamp_is_changed_before_file_fence(tmp_path, monkeypatch):
    payload = tmp_path / "payload.tar"
    with tarfile.open(payload, "w", format=tarfile.PAX_FORMAT) as outgoing:
        member = tarfile.TarInfo("a.jsonl")
        member.size = 1
        member.pax_headers = {"genesis.mtime_ns": "1234567890000000001",
                              "genesis.sha256": hashlib.sha256(b"x").hexdigest()}
        outgoing.addfile(member, io.BytesIO(b"x"))
    events = []
    utime, fsync = os.utime, os.fsync
    def timestamp(*args, **kwargs):
        events.append("timestamp")
        utime(*args, **kwargs)
    def fence(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            events.append("file-fence")
        fsync(fd)
    monkeypatch.setattr(archive.os, "utime", timestamp)
    monkeypatch.setattr(archive.os, "fsync", fence)
    assert archive.restore(payload, tmp_path / "restored", archive.object_name("a.jsonl"))
    assert events == ["timestamp", "file-fence"]
