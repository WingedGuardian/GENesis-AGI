"""Portable source archives preserve provenance and refuse unsafe payloads."""

import fcntl
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


def test_unchanged_capture_reused_but_cipher_corruption_recaptured(tmp_path, monkeypatch):
    root, destination, scratch = (tmp_path / name for name in ('sources', 'cipher', 'scratch'))
    for directory in (root, destination, scratch):
        directory.mkdir()
    source = root / 'a.jsonl'
    source.write_text('initial')
    calls = []
    def copy_crypt(source, target, password, **kwargs):
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


def test_filesystem_surrogate_path_roundtrip(tmp_path):
    relative = os.fsdecode(b'project/session-\xff.jsonl')
    source = tmp_path / os.fsdecode(b'source-\xff.jsonl')
    source.write_bytes(b'data')
    plain = tmp_path / 'capture.tar'
    archive.capture(source, relative, plain)
    assert archive.restore(plain, tmp_path / 'restored', archive.object_name(relative))
    assert (tmp_path / 'restored' / relative).read_bytes() == b'data'


def selection_setup(tmp_path, monkeypatch):
    root, cipher, scratch = (tmp_path / name for name in ('root', 'cipher', 'scratch'))
    for directory in (cipher, scratch):
        directory.mkdir()
    monkeypatch.setattr(archive, 'crypt', lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes()))
    plain, name = payload(tmp_path, relative='p/a.jsonl', content=b'v2')
    (cipher / name).write_bytes(plain.read_bytes())
    return root, cipher, scratch, name


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


def test_analytics_outside_home_relative_restore_and_exclusions(tmp_path, monkeypatch):
    source, scratch, destination = (tmp_path / name for name in ('outside', 'scratch', 'new-location'))
    source.mkdir()
    scratch.mkdir()
    for name in ('events/p.parquet', 'inventory.json', 'derived/cached.duckdb', '.staging/temp'):
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(name)
    monkeypatch.setattr(archive, 'crypt', lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes()))
    cipher = tmp_path / 'analytics.gpg'
    archive.analytics_archive(source, cipher, scratch, b'p')
    archive.analytics_restore(cipher, destination, scratch, b'p')
    assert (destination / 'events/p.parquet').read_text() == 'events/p.parquet'
    assert not (destination / 'derived').exists()
    assert not (destination / '.staging').exists()
    with pytest.raises(ValueError, match='exists'):
        archive.analytics_restore(cipher, destination, scratch, b'p')
    archive.analytics_restore(cipher, destination, scratch, b'p', force=True)
    assert list(tmp_path.glob('new-location.pre-restore-*'))


def test_analytics_refuses_links_before_cipher_publication(tmp_path, monkeypatch):
    source, scratch = tmp_path / 'data', tmp_path / 'scratch'
    source.mkdir()
    scratch.mkdir()
    (source / 'link').symlink_to(tmp_path)
    cipher = tmp_path / 'analytics.gpg'
    with pytest.raises(ValueError, match='link'):
        archive.analytics_archive(source, cipher, scratch, b'p')
    assert not cipher.exists()


@pytest.mark.parametrize('member', ['../escape', '/absolute', 'derived/cache', 'x//y'])
def test_analytics_refuses_unsafe_members(tmp_path, monkeypatch, member):
    cipher, _ = payload(tmp_path, relative=member)
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    monkeypatch.setattr(archive, 'crypt', lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes()))
    with pytest.raises(ValueError, match='unsafe'):
        archive.analytics_restore(cipher, tmp_path / 'restore', scratch, b'p')


def test_database_only_restore_does_not_take_analytics_lock(sandbox, monkeypatch):
    lock_dir = sandbox['home'] / '.genesis/locks'
    lock_dir.mkdir()
    monkeypatch.setenv('GENESIS_RESTORE_LOCK_WAIT', '0')
    with (lock_dir / 'transcript-analytics.lock').open('w') as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = _run(sandbox, extra_args=['--database-only'])
    assert 'analytics writer is busy' not in result.stdout
    assert not (lock_dir / 'transcript-analytics-restore-epoch').exists()


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
    command = 'source "$1"; _BACKEND=smb; _smb_run() { echo "$3"; }; backend_list_strict missing'
    # Function has its own positional arguments; capture the status outside it.
    command = command.replace('_smb_run() { echo "$3"; }', 'status="$3"; _smb_run() { echo "$status"; }')
    result = subprocess.run(["bash", "-c", command, "test", str(helper), str(tmp_path), status], capture_output=True)
    assert result.returncode == expected


def test_dryrun_restore_does_not_take_analytics_lock(sandbox, monkeypatch):
    lock_dir = sandbox["home"] / ".genesis/locks"
    lock_dir.mkdir()
    monkeypatch.setenv("GENESIS_RESTORE_LOCK_WAIT", "0")
    with (lock_dir / "transcript-analytics.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        result = _run(sandbox, extra_args=["--dry-run"])
    assert "analytics writer is busy" not in result.stdout
    assert not (lock_dir / "transcript-analytics-restore-epoch").exists()


def test_shell_dedicated_analytics_archive_restores_to_current_outside_home(sandbox, tmp_path, monkeypatch):
    _snapshot(sandbox, 'archive-host', _NEW)
    snapshot = sandbox['offsite'] / 'Genesis/archive-host' / _NEW
    extra = snapshot / 'extra'
    extra.mkdir()
    original = tmp_path / 'original-storage'
    original.mkdir()
    (original / 'inventory.json').write_text('{}')
    scratch = tmp_path / 'scratch'
    scratch.mkdir()
    archive.analytics_archive(original, extra / 'transcript-analytics-v1.tar.gpg', scratch, b'testpass')
    (snapshot / 'COMPLETE').write_text('genesis-snapshot 1\nextra transcript-analytics-v1.tar.gpg\n')
    current = tmp_path / 'current-storage'
    runtime = sandbox['gd'] / '.venv/bin/python'
    runtime.parent.mkdir(parents=True)
    runtime.write_text('#!/bin/sh\n[ "$3" = "--configured-enabled-data-dir" ] || exit 9\nprintf "%s\\n" "$ANALYTICS_TEST_DESTINATION"\n')
    runtime.chmod(0o700)
    monkeypatch.setenv('ANALYTICS_TEST_DESTINATION', str(current))
    result = _run(sandbox, host_override='archive-host')
    assert result.returncode == 0, result.stdout + result.stderr
    assert (current / 'inventory.json').read_text() == '{}'
    assert original.exists()



def test_capture_index_published_once_per_batch(tmp_path, monkeypatch):
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
    assert len(publications) == 1
    assert not archive.backup(root, destination, scratch, b'p')
    assert len(publications) == 1


def test_index_publication_failure_preserves_cipher_and_recaptures(tmp_path, monkeypatch):
    root, destination, scratch = (tmp_path / name for name in ('sources', 'cipher', 'scratch'))
    for directory in (root, destination, scratch):
        directory.mkdir()
    (root / 'a.jsonl').write_text('{}')
    real_replace = archive.os.replace
    def fail_index(source, target):
        if Path(target).name == '.capture-index.json':
            raise OSError('index unavailable')
        return real_replace(source, target)
    calls = []
    def copy_crypt(src, dst, *args, **kwargs):
        calls.append(True)
        dst.write_bytes(src.read_bytes())
    monkeypatch.setattr(archive, 'crypt', copy_crypt)
    monkeypatch.setattr(archive.os, 'replace', fail_index)
    assert archive.backup(root, destination, scratch, b'p')
    assert (destination / archive.object_name('a.jsonl')).exists()
    assert not (destination / '.capture-index.json').exists()
    monkeypatch.setattr(archive.os, 'replace', real_replace)
    assert not archive.backup(root, destination, scratch, b'p')
    assert len(calls) == 2


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
