"""Portable source archives preserve provenance and refuse unsafe payloads."""

import errno
import hashlib
import importlib.util
import io
import os
import subprocess
import tarfile
from pathlib import Path

import pytest

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








def test_unchanged_capture_reused_but_cipher_corruption_recaptured(tmp_path, monkeypatch):
    root, destination, scratch = (tmp_path / name for name in ('sources', 'cipher', 'scratch'))
    for directory in (root, destination, scratch):
        directory.mkdir()
    source = root / 'a.jsonl'
    source.write_text('initial')
    calls = []
    def copy_crypt(source, target, password, **kwargs):
        if not kwargs.get("decrypt") and source.name == "source.tar":
            calls.append(source)
        target.write_bytes(source.read_bytes())
    monkeypatch.setattr(archive, 'crypt', copy_crypt)
    assert not archive.backup(root, destination, scratch, b'p')
    assert not archive.backup(root, destination, scratch, b'p')
    assert len(calls) == 1
    cipher = destination / archive.object_name('a.jsonl')
    cipher.write_bytes(b'corrupt')
    assert not archive.backup(root, destination, scratch, b'p')
    assert len(calls) == 2
    original_mtime = source.stat().st_mtime_ns
    source.write_text('changed')
    os.utime(source, ns=(original_mtime, original_mtime))
    assert not archive.backup(root, destination, scratch, b'p')
    assert len(calls) == 3


def test_main_scope_writes_v2_and_retains_other_projects(tmp_path, monkeypatch):
    root, destination, scratch = (tmp_path / name for name in ('sources', 'cipher', 'scratch'))
    for directory in (root, destination, scratch):
        directory.mkdir()
    for name in ('main/a.jsonl', 'other/a.jsonl', 'main/subagents/agent-x.jsonl'):
        source = root / name
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text('{}')
    monkeypatch.setattr(archive, 'crypt', lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes()))
    assert not archive.backup(root, destination, scratch, b'p')
    old = destination / archive.object_name('other/a.jsonl')
    before = old.read_bytes()
    assert not archive.backup(root, destination, scratch, b'p', project='main')
    assert old.read_bytes() == before
    assert (destination / archive.object_name('main/a.jsonl')).exists()
    assert not list(destination.glob('*.jsonl.gpg'))


@pytest.mark.parametrize("project", [None, "main"])
@pytest.mark.parametrize("retained", ["absent", "valid", "corrupt", "missing"])
def test_absent_source_root_still_validates_retained_history(tmp_path, monkeypatch, project, retained):
    root, destination, scratch = (tmp_path / name for name in ('sources', 'cipher', 'scratch'))
    for directory in (destination, scratch):
        directory.mkdir()
    monkeypatch.setattr(archive, 'crypt', lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes()))
    if retained != "absent":
        source = root / 'main/a.jsonl'
        source.parent.mkdir(parents=True)
        source.write_text('{}')
        assert not archive.backup(root, destination, scratch, b'p', project=project)
        source.unlink()
        source.parent.rmdir()
        root.rmdir()
        capture = destination / archive.object_name('main/a.jsonl')
        if retained == "corrupt":
            capture.write_bytes(b'corrupt')
        elif retained == "missing":
            capture.unlink()
    assert archive.backup(root, destination, scratch, b'p', project=project) == (retained in ("corrupt", "missing"))


@pytest.mark.parametrize("kind", ["file", "dangling-link", "parent-link-loop"])
def test_invalid_source_root_remains_incomplete(tmp_path, monkeypatch, kind):
    root, destination, scratch = (tmp_path / name for name in ('sources', 'cipher', 'scratch'))
    destination.mkdir()
    scratch.mkdir()
    monkeypatch.setattr(archive, 'crypt', lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes()))
    if kind == "file":
        root.write_text('{}')
    elif kind == "dangling-link":
        root.symlink_to(tmp_path / 'absent')
    else:
        loop = tmp_path / 'loop'
        loop.symlink_to('loop')
        root = loop / 'sources'
    assert archive.backup(root, destination, scratch, b'p')


@pytest.mark.parametrize("error", [errno.EACCES, errno.ELOOP, errno.EIO])
@pytest.mark.parametrize("project", [None, "main"])
def test_source_inspection_failure_is_not_empty_scope(tmp_path, monkeypatch, error, project):
    root, destination, scratch = (tmp_path / name for name in ('sources', 'cipher', 'scratch'))
    for directory in (root, destination, scratch):
        directory.mkdir()
    if project is not None:
        (root / project).mkdir()
    inspected = root if project is None else root / project
    monkeypatch.setattr(archive, 'crypt', lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes()))
    for method in ('stat', 'lstat'):
        original = getattr(Path, method)
        def unavailable(path, *args, _original=original, **kwargs):
            if path == inspected:
                raise OSError(error, 'injected source inspection failure')
            return _original(path, *args, **kwargs)
        monkeypatch.setattr(Path, method, unavailable)
    assert archive.backup(root, destination, scratch, b'p', project=project)




def selection_setup(tmp_path, monkeypatch):
    root, cipher, scratch = (tmp_path / name for name in ('root', 'cipher', 'scratch'))
    for directory in (cipher, scratch):
        directory.mkdir()
    monkeypatch.setattr(archive, 'crypt', lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes()))
    plain, name = payload(tmp_path, relative='p/a.jsonl', content=b'v2')
    (cipher / name).write_bytes(plain.read_bytes())
    return root, cipher, scratch, name





























def test_capture_index_publishes_first_then_remaining_batch(tmp_path, monkeypatch):
    root, destination, scratch = (tmp_path / name for name in ('sources', 'cipher', 'scratch'))
    for directory in (root, destination, scratch):
        directory.mkdir()
    for number in range(4):
        (root / f'{number}.jsonl').write_text('{}')
    monkeypatch.setattr(archive, 'crypt', lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes()))
    real_dump = archive.json.dump
    publications = []
    def count_dump(*args, **kwargs):
        publications.append(True)
        return real_dump(*args, **kwargs)
    monkeypatch.setattr(archive.json, 'dump', count_dump)
    assert not archive.backup(root, destination, scratch, b'p')
    assert len(publications) == 2
    assert not archive.backup(root, destination, scratch, b'p')
    assert len(publications) == 2


def test_index_publication_failure_preserves_cipher_and_recaptures(tmp_path, monkeypatch):
    root, destination, scratch = (tmp_path / name for name in ('sources', 'cipher', 'scratch'))
    for directory in (root, destination, scratch):
        directory.mkdir()
    (root / 'a.jsonl').write_text('{}')
    real_replace = archive.os.replace
    def fail_index(source, target):
        if Path(target).name == archive._INDEX_NAME:
            raise OSError('index unavailable')
        return real_replace(source, target)
    calls = []
    def copy_crypt(src, dst, *args, **kwargs):
        if not kwargs.get("decrypt") and src.name == "source.tar":
            calls.append(True)
        dst.write_bytes(src.read_bytes())
    monkeypatch.setattr(archive, 'crypt', copy_crypt)
    monkeypatch.setattr(archive.os, 'replace', fail_index)
    assert archive.backup(root, destination, scratch, b'p')
    assert (destination / archive.object_name('a.jsonl')).exists()
    assert not (destination / archive._INDEX_NAME).exists()
    monkeypatch.setattr(archive.os, 'replace', real_replace)
    assert not archive.backup(root, destination, scratch, b'p')
    assert len(calls) == 2




def test_backup_cli_accepts_dash_prefixed_main_project(tmp_path):
    root, destination, scratch = (tmp_path / name for name in ('sources', 'cipher', 'scratch'))
    for directory in (root, destination, scratch):
        directory.mkdir()
    (root / '-main').mkdir()
    (root / '-main/a.jsonl').write_text('{}')
    helper = Path(__file__).parents[2] / 'scripts/lib/transcript_archive.py'
    result = subprocess.run(['python3', str(helper), 'backup', str(root),
                             '--destination', str(destination), '--scratch', str(scratch),
                             '--project=-main'], input=b'testpass', capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (destination / archive.object_name('-main/a.jsonl')).exists()
