"""Portable source archives preserve provenance and refuse unsafe payloads."""

import hashlib
import importlib.util
import io
import os
import subprocess
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

def selection_setup(tmp_path, monkeypatch):
    root, cipher, scratch = (tmp_path / name for name in ('root', 'cipher', 'scratch'))
    for directory in (cipher, scratch):
        directory.mkdir()
    monkeypatch.setattr(archive, 'crypt', lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes()))
    plain, name = payload(tmp_path, relative='p/a.jsonl', content=b'v2')
    (cipher / name).write_bytes(plain.read_bytes())
    return root, cipher, scratch, name

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

def test_filesystem_surrogate_path_roundtrip(tmp_path):
    relative = os.fsdecode(b'project/session-\xff.jsonl')
    source = tmp_path / os.fsdecode(b'source-\xff.jsonl')
    source.write_bytes(b'data')
    plain = tmp_path / 'capture.tar'
    archive.capture(source, relative, plain)
    assert archive.restore(plain, tmp_path / 'restored', archive.object_name(relative))
    assert (tmp_path / 'restored' / relative).read_bytes() == b'data'

def test_incomparable_legacy_v2_is_conflict_even_force(tmp_path, monkeypatch, capsys):
    root, cipher, scratch, name = selection_setup(tmp_path, monkeypatch)
    (cipher / 'a.jsonl.gpg').write_bytes(b'legacy')
    assert archive.restore_set(cipher, root, 'p', scratch, b'p', force=True)
    assert not root.exists()
    assert 'incomparable' in capsys.readouterr().err
    assert not archive.restore_set(cipher, root, 'p', scratch, b'p', preferences=['p/a.jsonl=legacy'])
    assert (root / 'p/a.jsonl').read_bytes() == b'legacy'

def test_freshness_uses_recorded_mtime_not_cipher_mtime(tmp_path, monkeypatch):
    root, cipher, scratch, name = selection_setup(tmp_path, monkeypatch)
    destination = root / 'p/a.jsonl'
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b'newer existing')
    os.utime(destination, ns=(1234567891000000000, 1234567891000000000))
    # Download/cache timestamps must not override the archived source timestamp.
    os.utime(cipher / name, ns=(2234567891000000000, 2234567891000000000))
    assert not archive.restore_set(cipher, root, 'p', scratch, b'p')
    assert destination.read_bytes() == b'newer existing'
    assert not archive.restore_set(cipher, root, 'p', scratch, b'p', force=True)
    assert destination.read_bytes() == b'v2'

def test_invalid_v2_falls_back_to_valid_legacy_reports_incomplete(tmp_path, monkeypatch):
    root, cipher, scratch, name = selection_setup(tmp_path, monkeypatch)
    (cipher / name).write_bytes(b'bad archive')
    (cipher / 'a.jsonl.gpg').write_bytes(b'valid legacy')
    assert archive.restore_set(cipher, root, 'p', scratch, b'p')
    assert (root / 'p/a.jsonl').read_bytes() == b'valid legacy'

def test_digest_checked_during_dry_run(tmp_path):
    plain, name = payload(tmp_path)
    with tarfile.open(plain, 'r') as tar:
        member = tar.getmembers()[0]
    with tarfile.open(plain, 'w') as tar:
        tar.addfile(member, io.BytesIO(b'bad\n'))
    with pytest.raises(ValueError, match='digest'):
        archive.restore(plain, tmp_path / 'restore', name, dry_run=True)

def test_selected_inventory_excludes_conflicting_stale_cipher(tmp_path, monkeypatch):
    root, cipher, scratch, name = selection_setup(tmp_path, monkeypatch)
    (cipher / 'a.jsonl.gpg').write_bytes(b'stale')
    assert not archive.restore_set(cipher, root, 'p', scratch, b'p', selected={name})
    assert (root / 'p/a.jsonl').read_bytes() == b'v2'

@pytest.mark.parametrize("failure,expected", [("absent", 3), ("timeout", 1), ("denied", 1)])
def test_strict_local_listing_distinguishes_absence(tmp_path, failure, expected):
    helper = Path(__file__).parents[2] / "scripts/lib/backup_backends.sh"
    function = {
        "absent": 'echo "ls: cannot access x: No such file or directory" >&2; return 2',
        "timeout": 'return 124',
        "denied": 'echo "Permission denied" >&2; return 2',
    }[failure]
    command = 'source "$1"; _BACKEND=local; _BACKEND_LOCAL_ROOT="$2"; _t_ctl() { ' + function + '; }; backend_list_strict missing'
    result = subprocess.run(["bash", "-c", command, "test", str(helper), str(tmp_path)], capture_output=True)
    assert result.returncode == expected

@pytest.mark.parametrize("status,expected", [("NT_STATUS_OBJECT_PATH_NOT_FOUND", 3),
                                             ("NT_STATUS_ACCESS_DENIED", 1),
                                             ("NT_STATUS_CONNECTION_DISCONNECTED", 1)])
def test_strict_smb_listing_refuses_failed_cd(tmp_path, status, expected):
    helper = Path(__file__).parents[2] / "scripts/lib/backup_backends.sh"
    command = ('source "$1"; _BACKEND=smb; status="$3"; '
               '_smb_run() { [ "$*" = "-D missing -c ls" ] || return 99; '
               'printf "cd missing: %s\\n" "$status"; return 1; }; '
               'backend_list_strict missing')
    result = subprocess.run(["bash", "-c", command, "test", str(helper), str(tmp_path), status], capture_output=True)
    assert result.returncode == expected

def test_shell_preference_accepts_dash_prefixed_project(sandbox, tmp_path):
    _snapshot(sandbox, 'archive-host', _NEW)
    selected = sandbox['offsite'] / 'Genesis/archive-host' / _NEW / 'transcripts'
    selected.mkdir()
    source = tmp_path / 'source'
    source.mkdir()
    project = str(sandbox['gd']).replace('/', '-')
    (source / project).mkdir()
    (source / project / 'a.jsonl').write_text('v2')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    assert not archive.backup(source, selected, scratch, b'testpass')
    legacy = tmp_path / 'legacy.jsonl'
    legacy.write_text('preferred legacy')
    archive.crypt(legacy, selected / 'a.jsonl.gpg', b'testpass')
    result = _run(sandbox, host_override='archive-host',
                  extra_args=['--transcript-preference', project + '/a.jsonl=legacy'])
    assert result.returncode == 0, result.stdout + result.stderr
    assert (sandbox['home'] / '.claude/projects' / project / 'a.jsonl').read_text() == 'preferred legacy'

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

@pytest.mark.parametrize("failure", ["wrong-key", "literal"])
def test_archive_crypto_preserves_native_errors_and_rejects_exit_zero(capture_set, monkeypatch, failure):
    root, destination, _ = capture_set
    source, cipher, target = root / "a.jsonl", destination / "payload.gpg", root / "restored"
    actual = subprocess.run
    if failure == "literal":
        actual(["gpg", "--no-options", "--batch", "--store", "--output", str(cipher), str(source)],
               capture_output=True, check=True)
    else:
        archive.crypt(source, cipher, b"current-test-key")
    target.write_bytes(b"previous destination")
    results = []

    def invoked(*args, **kwargs):
        result = actual(*args, **kwargs)
        results.append(result)
        return result

    monkeypatch.setattr(archive.subprocess, "run", invoked)
    try:
        expected = subprocess.CalledProcessError if failure == "wrong-key" else ValueError
        with pytest.raises(expected) as rejected:
            archive.crypt(cipher, target, b"wrong-test-key", decrypt=True)
        assert len(results) == 1
        if failure == "wrong-key":
            assert rejected.value.returncode == results[0].returncode != 0
            assert rejected.value.cmd == results[0].args
        else:
            assert results[0].returncode == 0
        assert target.read_bytes() == b"previous destination"
        assert not list(target.parent.glob(".gpg-restore-*"))
    finally:
        actual(["gpgconf", "--homedir", str(root.parent / "gpg"), "--kill", "gpg-agent"],
               capture_output=True, check=False)


@pytest.mark.parametrize("force", [False, True])
def test_legacy_existing_destination_requires_explicit_force(tmp_path, monkeypatch, force):
    root, cipher, scratch, _ = selection_setup(tmp_path, monkeypatch)
    for path in cipher.iterdir():
        path.unlink()
    (cipher / "a.jsonl.gpg").write_bytes(b"legacy backup")
    target = root / "p/a.jsonl"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"existing")
    os.utime(target, ns=(0, 0))
    assert not archive.restore_set(cipher, root, "p", scratch, b"p", force=force)
    assert target.read_bytes() == (b"legacy backup" if force else b"existing")
