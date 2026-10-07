"""Portable source archives preserve provenance and refuse unsafe payloads."""

import fcntl
import hashlib
import importlib.util
import io
import os
import tarfile
from pathlib import Path

import pytest

from tests.test_scripts.test_restore_offsite_pull import _NEW, _run, _snapshot
from tests.test_scripts.test_restore_offsite_pull import sandbox as sandbox

SPEC = importlib.util.spec_from_file_location(
    "transcript_archive", Path(__file__).parents[2] / "scripts/lib/transcript_archive.py"
)
archive = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(archive)


def payload(tmp_path, relative="project/subagents/agent-a.jsonl", content=b"raw\n", kind=None):
    path = tmp_path / "source.tar"
    with tarfile.open(path, "w") as out:
        info = tarfile.TarInfo(relative)
        info.size = len(content)
        info.pax_headers = {
            "genesis.mtime_ns": "1234567890000000000",
            "genesis.sha256": hashlib.sha256(content).hexdigest(),
        }
        if kind is not None:
            info.type = kind
            info.size = 0
        out.addfile(info, io.BytesIO(content))
    return path, archive.object_name(relative)


def test_discovery_all_projects_metadata_no_links(tmp_path):
    (tmp_path / "p/subagents").mkdir(parents=True)
    for name in (
        "p/main.jsonl",
        "p/subagents/agent-x.jsonl",
        "p/subagents/agent-x.meta.json",
        "p/ignored.json",
    ):
        (tmp_path / name).write_text("{}")
    (tmp_path / "p/link.jsonl").symlink_to(tmp_path / "p/main.jsonl")
    (tmp_path / "alias").symlink_to(tmp_path / "p", target_is_directory=True)
    assert {p.relative_to(tmp_path).as_posix() for p in archive.sources(tmp_path)} == {
        "p/main.jsonl",
        "p/subagents/agent-x.jsonl",
        "p/subagents/agent-x.meta.json",
    }


def test_capture_and_restore_mtime_force_and_dryrun(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_bytes(b'{"message":"hello"}\n')
    os.utime(source, ns=(1234567890000000001, 1234567890000000001))
    tar = tmp_path / "captured.tar"
    relative = "project/session.jsonl"
    archive.capture(source, relative, tar)
    root = tmp_path / "restored"
    name = archive.object_name(relative)
    assert archive.restore(tar, root, name, dry_run=True)
    assert not root.exists()
    assert archive.restore(tar, root, name)
    target = root / relative
    assert target.read_bytes() == source.read_bytes()
    assert target.stat().st_mtime_ns == source.stat().st_mtime_ns
    target.write_text("newer")
    assert not archive.restore(tar, root, name)
    assert archive.restore(tar, root, name, force=True)


@pytest.mark.parametrize(
    "relative", ["../escape.jsonl", "/absolute.jsonl", "p/../x.jsonl", "p//x.jsonl"]
)
def test_unsafe_names(tmp_path, relative):
    path, name = payload(tmp_path, relative)
    with pytest.raises(ValueError):
        archive.restore(path, tmp_path / "root", name)


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE]
)
def test_nonregular_member(tmp_path, kind):
    path, name = payload(tmp_path, kind=kind)
    with pytest.raises(ValueError):
        archive.restore(path, tmp_path / "root", name)


def test_multiple_members_and_wrong_identity(tmp_path):
    path, name = payload(tmp_path)
    with pytest.raises(ValueError):
        archive.restore(path, tmp_path / "root", "v2-wrong.tar.gpg")
    with tarfile.open(path, "a") as out:
        out.addfile(tarfile.TarInfo("other"))
    with pytest.raises(ValueError):
        archive.restore(path, tmp_path / "root", name)


def test_destination_symlink(tmp_path):
    path, name = payload(tmp_path)
    root = tmp_path / "root"
    root.mkdir()
    (root / "project").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        archive.restore(path, root, name)


def test_encrypt_failure_keeps_last_good_and_cleans(tmp_path, monkeypatch):
    source, destination, scratch = [tmp_path / p for p in ("sources", "archives", "scratch")]
    for p in (source, destination, scratch):
        p.mkdir()
    (source / "a.jsonl").write_text("data")
    old = destination / archive.object_name("a.jsonl")
    old.write_bytes(b"lastgood")

    def fail(*args, **kwargs):
        raise OSError("encrypt failure")

    monkeypatch.setattr(archive, "crypt", fail)
    assert archive.backup(source, destination, scratch, b"password")
    assert old.read_bytes() == b"lastgood"
    assert list(destination.iterdir()) == [old]
    assert not list(scratch.iterdir())


def test_real_encrypted_roundtrip_keeps_vanished_source(tmp_path):
    source, destination, scratch = [tmp_path / p for p in ("sources", "archives", "scratch")]
    for p in (source, destination, scratch):
        p.mkdir()
    (source / "a.jsonl").write_bytes(b"private context\n")
    assert not archive.backup(source, destination, scratch, b"test-only-passphrase")
    encrypted = destination / archive.object_name("a.jsonl")
    assert b"private context" not in encrypted.read_bytes()
    (source / "a.jsonl").unlink()
    assert not archive.backup(source, destination, scratch, b"test-only-passphrase")
    plain = scratch / "plain.tar"
    archive.crypt(encrypted, plain, b"test-only-passphrase", decrypt=True)
    assert archive.restore(plain, tmp_path / "restore", encrypted.name)
    assert (tmp_path / "restore/a.jsonl").read_bytes() == b"private context\n"


@pytest.mark.parametrize(
    "change,accepted",
    [("append", True), ("rewrite", False), ("truncate", False), ("replace", False)],
)
def test_live_prefix_contract(tmp_path, monkeypatch, change, accepted):
    source = tmp_path / "live.jsonl"
    source.write_bytes(b"original\n")
    original_fdopen = os.fdopen

    class InterleavedReader:
        def __init__(self, raw):
            self.raw = raw

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.raw.close()

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def seek(self, offset):
            if change == "append":
                with source.open("ab") as writer:
                    writer.write(b"appended\n")
            elif change == "replace":
                source.unlink()
                source.write_bytes(b"replacement\n")
            else:
                source.write_bytes(b"rewritten" if change == "rewrite" else b"x")
            return self.raw.seek(offset)

    monkeypatch.setattr(
        os,
        "fdopen",
        lambda fd, mode, **kwargs: InterleavedReader(original_fdopen(fd, mode, **kwargs)),
    )
    target = tmp_path / "capture.tar"
    if not accepted:
        with pytest.raises(ValueError):
            archive.capture(source, "p/live.jsonl", target)
    else:
        archive.capture(source, "p/live.jsonl", target)
        with tarfile.open(target) as captured:
            assert captured.extractfile(captured.getmembers()[0]).read() == b"original\n"


def test_shell_selected_snapshot_ignores_stale_cache(sandbox, tmp_path):
    _snapshot(sandbox, "archive-host", _NEW)
    selected = sandbox["offsite"] / "Genesis/archive-host" / _NEW / "transcripts"
    selected.mkdir()
    cache = sandbox["backup"] / "transcripts"
    cache.mkdir()
    root = tmp_path / "inputs"
    scratch = tmp_path / "scratch"
    root.mkdir()
    scratch.mkdir()
    (root / "selected.jsonl").write_text("selected source")
    assert not archive.backup(root, selected, scratch, b"testpass")
    (root / "selected.jsonl").unlink()
    (root / "stale.jsonl").write_text("stale source")
    assert not archive.backup(root, cache, scratch, b"testpass")
    result = _run(sandbox, host_override="archive-host")
    assert result.returncode == 0, result.stdout + result.stderr
    restored = sandbox["home"] / ".claude/projects"
    assert (restored / "selected.jsonl").read_text() == "selected source"
    assert not (restored / "stale.jsonl").exists()


@pytest.mark.parametrize(
    "lock_name,diagnostic",
    [
        ("transcript-analytics.lock", "analytics writer is busy"),
        ("transcript-analytics-publication.lock", "analytics readers are busy"),
    ],
)
def test_restore_respects_analytics_locks(sandbox, monkeypatch, lock_name, diagnostic):
    lock_dir = sandbox["home"] / ".genesis/locks"
    lock_dir.mkdir()
    monkeypatch.setenv("GENESIS_RESTORE_LOCK_WAIT", "0")
    with (lock_dir / lock_name).open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = _run(sandbox)
    assert result.returncode != 0
    assert diagnostic in result.stdout
    assert not (sandbox["gd"] / "data/genesis.db").exists()


def test_fresh_restore_needs_no_analytics_venv_and_invalidates_before_mutation(sandbox):
    config_module = sandbox["gd"] / "src/genesis/transcript_analytics/config.py"
    config_module.parent.mkdir(parents=True)
    config_module.write_text("raise AssertionError('recovery must not import this')\n")
    _snapshot(sandbox, "archive-host", _NEW)
    result = _run(sandbox, host_override="archive-host")
    assert result.returncode == 0, result.stdout + result.stderr
    epoch = sandbox["home"] / ".genesis/locks/transcript-analytics-restore-epoch"
    assert epoch.read_text().strip()
    assert not (sandbox["gd"] / ".venv").exists()
