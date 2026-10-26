"""Immutable transcript transfer, authenticated selection and fail-closed collection."""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tests.test_scripts.test_backup_dated_snapshots import _run_local
from tests.test_scripts.test_backup_dated_snapshots import backup_env as backup_env
from tests.test_scripts.test_restore_offsite_pull import _NEW, _run, _snapshot
from tests.test_scripts.test_restore_offsite_pull import sandbox as sandbox
from tests.test_scripts.test_transcript_archive import archive

ROOT = Path(__file__).parents[2]
HOST = "Genesis/archive-host"
SNAPSHOT = HOST + "/" + _NEW


@pytest.mark.parametrize("backend", ["local", "smb"])
@pytest.mark.parametrize("state", ["complete", "incomplete", "listing-error"])
def test_backup_refuses_snapshot_reuse_before_any_offsite_write(
    backup_env, tmp_path, backend, state
):
    stamp = "20261007T120000Z"
    (backup_env["bind"] / "date").write_text(
        '#!/bin/bash\nif [ "$*" = "-u +%Y%m%dT%H%M%SZ" ]; then echo '
        + stamp
        + '; else exec /usr/bin/date "$@"; fi\n'
    )
    (backup_env["bind"] / "date").chmod(0o755)
    offsite = tmp_path / "offsite"
    snapshot = offsite / HOST / stamp
    snapshot.mkdir(parents=True)
    (snapshot / "manifest").write_bytes(b"original inventory")
    if state == "complete":
        (snapshot / "COMPLETE").write_bytes(b"genesis-snapshot 1\n")
    before = {
        str(p.relative_to(offsite)): p.read_bytes() for p in offsite.rglob("*") if p.is_file()
    }
    if backend == "smb":
        message = "NT_STATUS_ACCESS_DENIED" if state == "listing-error" else ""
        (backup_env["bind"] / "smbclient").write_text(
            '#!/bin/bash\nprev=""\nfor a in "$@"; do\n'
            + f'if [ "$prev" = "-c" ]; then printf "%s\\n" "$a" >> "{backup_env["smb_log"]}"; fi\n'
            + 'prev="$a"\ndone\n'
            + f'printf "%s\\n" "{message}"\nexit 0\n'
        )
    elif state == "listing-error":
        (backup_env["bind"] / "ls").write_text(
            '#!/bin/bash\necho "permission denied" >&2\nexit 2\n'
        )
        (backup_env["bind"] / "ls").chmod(0o755)
    result = _run_local(
        backup_env,
        offsite,
        {
            "GENESIS_BACKUP_NAS_HOST": "archive-host",
            "GENESIS_BACKUP_TIER2_BACKEND": backend,
            "GENESIS_BACKUP_NAS": "//example/share",
            "GENESIS_BACKUP_NAS_USER": "test",
            "GENESIS_BACKUP_NAS_PASS": "test",
        },
    )
    assert result.returncode != 0, result.stdout + result.stderr
    expected = (
        "cannot verify off-site snapshot absence"
        if state == "listing-error"
        else "off-site snapshot already exists"
    )
    assert expected in result.stdout
    assert before == {
        str(p.relative_to(offsite)): p.read_bytes() for p in offsite.rglob("*") if p.is_file()
    }
    if backend == "smb":
        commands = backup_env["smb_log"].read_text()
        assert "put " not in commands and "mkdir " not in commands and "rename " not in commands


def setup_pool(tmp_path):
    captures, backend, scratch = [tmp_path / n for n in ("captures", "backend", "scratch")]
    for p in (captures, backend, scratch):
        p.mkdir()
    source = tmp_path / "a.jsonl"
    source.write_text("original")
    plain = tmp_path / "source.tar"
    archive.capture(source, "p/a.jsonl", plain)
    archive.crypt(plain, captures / archive.object_name("p/a.jsonl"), b"testpass")
    (backend / SNAPSHOT / "transcripts").mkdir(parents=True)
    return captures, backend, scratch


def shell_pool(captures, backend, scratch, commands):
    script = (
        """set -euo pipefail
_SCRIPT_DIR="$1/scripts"; GENESIS_BIG_TMP="$2"; _BACKUP_PASSPHRASE=testpass
GENESIS_BACKUP_TIER2_BACKEND=local; GENESIS_BACKUP_LOCAL_PATH="$3"
source "$_SCRIPT_DIR/lib/backup_backends.sh"
source "$_SCRIPT_DIR/lib/transcript_pool.sh"
backend_init
captures="$4"
"""
        + commands
    )
    return subprocess.run(
        ["bash", "-c", script, "test", str(ROOT), str(scratch), str(backend), str(captures)],
        capture_output=True,
        text=True,
    )


def test_unchanged_corpus_transfers_no_objects_and_changed_capture_only_one(tmp_path):
    captures, backend, scratch = setup_pool(tmp_path)
    result = shell_pool(
        captures, backend, scratch, f'transcript_pool_backup "$captures" {HOST} {SNAPSHOT}'
    )
    assert result.returncode == 0, result.stderr
    objects = backend / HOST / "transcript-objects"
    initial = {p.name: p.stat().st_mtime_ns for p in objects.iterdir()}
    second = HOST + "/20260618T180000Z"
    (backend / second / "transcripts").mkdir(parents=True)
    result = shell_pool(
        captures, backend, scratch, f'transcript_pool_backup "$captures" {HOST} {second}'
    )
    assert result.returncode == 0, result.stderr
    assert "uploaded" not in result.stderr
    assert {p.name: p.stat().st_mtime_ns for p in objects.iterdir()} == initial
    current = next(captures.glob("*.gpg"))
    current.write_bytes(current.read_bytes() + b"changed fixture")
    third = HOST + "/20260619T180000Z"
    (backend / third / "transcripts").mkdir(parents=True)
    result = shell_pool(
        captures, backend, scratch, f'transcript_pool_backup "$captures" {HOST} {third}'
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("uploaded") == 1
    assert len(list(objects.iterdir())) == 2
    assert all((objects / n).stat().st_mtime_ns == t for n, t in initial.items())


def test_pooled_selected_snapshot_roundtrip_ignores_stale_local_cache(sandbox, tmp_path):
    _snapshot(sandbox, "archive-host", _NEW)
    captures, unused_backend, scratch = setup_pool(tmp_path)
    # Transfer against the fixture backend, whose snapshot includes a valid DB.
    (sandbox["offsite"] / SNAPSHOT / "transcripts").mkdir()
    result = shell_pool(
        captures,
        sandbox["offsite"],
        scratch,
        f'transcript_pool_backup "$captures" {HOST} {SNAPSHOT}',
    )
    assert result.returncode == 0, result.stderr
    cache = sandbox["backup"] / "transcripts"
    cache.mkdir()
    stale = cache / archive.object_name("p/stale.jsonl")
    stale.write_bytes(next(captures.glob("*.gpg")).read_bytes())
    result = _run(sandbox, host_override="archive-host")
    assert result.returncode == 0, result.stdout + result.stderr
    assert (sandbox["home"] / ".claude/projects/p/a.jsonl").read_text() == "original"
    assert not (sandbox["home"] / ".claude/projects/p/stale.jsonl").exists()


@pytest.mark.parametrize(
    "corruption", ["object", "manifest", "missing_object", "replayed_manifest"]
)
def test_corrupt_pooled_capture_fails_without_replacing_last_good_cache(tmp_path, corruption):
    captures, backend, scratch = setup_pool(tmp_path)
    result = shell_pool(
        captures, backend, scratch, f'transcript_pool_backup "$captures" {HOST} {SNAPSHOT}'
    )
    assert result.returncode == 0
    manifest = backend / SNAPSHOT / "transcripts/manifest-v1.json.gpg"
    object_path = next((backend / HOST / "transcript-objects").iterdir())
    if corruption == "object":
        object_path.write_bytes(b"corrupt")
    elif corruption == "missing_object":
        object_path.unlink()
    elif corruption == "manifest":
        manifest.write_bytes(b"corrupt")
    else:
        rows = archive.pool_read(manifest, SNAPSHOT, scratch, b"testpass")
        plain = scratch / "replay.json"
        plain.write_text(
            json.dumps({"version": 1, "snapshot": HOST + "/20260618T180000Z", "captures": rows})
        )
        archive.crypt(plain, manifest, b"testpass")
    cache = tmp_path / "cache"
    cache.mkdir()
    last_good = cache / next(captures.glob("*.gpg")).name
    last_good.write_bytes(b"last good")
    result = shell_pool(
        captures, backend, scratch, f'transcript_pool_pull {HOST} {SNAPSHOT} "{cache}"'
    )
    assert result.returncode != 0
    assert last_good.read_bytes() == b"last good"
    assert not list(scratch.glob("transcript-pool.*"))


def test_failed_atomic_local_upload_does_not_publish_partial_object(tmp_path):
    captures, backend, scratch = setup_pool(tmp_path)
    result = shell_pool(
        captures,
        backend,
        scratch,
        """_local_put() { mkdir -p "$(dirname "$_BACKEND_LOCAL_ROOT/$2")"; printf broken >"$_BACKEND_LOCAL_ROOT/$2"; return 1; }
backend_put_atomic "$captures/$(ls "$captures")" Genesis/archive-host/object.gpg""",
    )
    assert result.returncode != 0
    assert not (backend / HOST / "object.gpg").exists()
    assert not list((backend / HOST).glob(".partial-*"))


def test_smb_error_status_with_success_exit_does_not_publish_partial_object(tmp_path):
    captures, backend, scratch = setup_pool(tmp_path)
    result = shell_pool(
        captures,
        backend,
        scratch,
        """_BACKEND=smb
_smb_run() { case "$*" in *put*) echo NT_STATUS_DISK_FULL; return 0 ;; *rename*) echo unexpected-rename; return 0 ;; *) return 0 ;; esac; }
backend_put_atomic "$captures/$(ls "$captures")" Genesis/archive-host/object.gpg""",
    )
    assert result.returncode != 0
    assert "unexpected-rename" not in result.stdout


def test_gc_retains_snapshot_and_incomplete_manifest_refs_then_collects_expired_versions(tmp_path):
    captures, backend, scratch = setup_pool(tmp_path)
    current_file = next(captures.glob("*.gpg"))
    old_ns = int((time.time() - 10 * 86400) * 1e9)
    os.utime(current_file, ns=(old_ns, old_ns))
    assert (
        shell_pool(
            captures, backend, scratch, f'transcript_pool_backup "$captures" {HOST} {SNAPSHOT}'
        ).returncode
        == 0
    )
    old_object = next((backend / HOST / "transcript-objects").iterdir())
    current_file.write_bytes(current_file.read_bytes() + b"new revision")
    second_stamp = "20260618T180000Z"
    second = HOST + "/" + second_stamp
    (backend / second / "transcripts").mkdir(parents=True)
    assert (
        shell_pool(
            captures, backend, scratch, f'transcript_pool_backup "$captures" {HOST} {second}'
        ).returncode
        == 0
    )
    # The first snapshot has no COMPLETE marker: its authenticated refs still hold.
    assert (
        shell_pool(
            captures, backend, scratch, f"transcript_pool_gc {HOST} {second_stamp}"
        ).returncode
        == 0
    )
    assert old_object.exists()
    import shutil

    shutil.rmtree(backend / SNAPSHOT)
    assert (
        shell_pool(
            captures, backend, scratch, f"transcript_pool_gc {HOST} {second_stamp}"
        ).returncode
        == 0
    )
    assert not old_object.exists()
    assert len(list((backend / HOST / "transcript-objects").iterdir())) == 1


@pytest.mark.parametrize("uncertainty", ["missing_manifest", "bad_manifest", "listing_failure"])
def test_gc_aborts_on_uncertainty_before_any_deletion(tmp_path, uncertainty):
    captures, backend, scratch = setup_pool(tmp_path)
    assert (
        shell_pool(
            captures, backend, scratch, f'transcript_pool_backup "$captures" {HOST} {SNAPSHOT}'
        ).returncode
        == 0
    )
    orphan = backend / HOST / "transcript-objects" / ("1-" + "f" * 64 + ".gpg")
    orphan.write_bytes(b"obsolete")
    manifest = backend / SNAPSHOT / "transcripts/manifest-v1.json.gpg"
    prefix = ""
    if uncertainty == "missing_manifest":
        manifest.unlink()
    elif uncertainty == "bad_manifest":
        manifest.write_bytes(b"bad")
    else:
        prefix = "backend_list_dirs_strict() { return 1; }; "
    result = shell_pool(captures, backend, scratch, prefix + f"transcript_pool_gc {HOST} {_NEW}")
    assert result.returncode != 0
    assert orphan.exists()


def test_gc_protects_future_captures_and_ignores_partial_uploads(tmp_path):
    refs = tmp_path / "refs"
    refs.write_text("")
    names = tmp_path / "names"
    future = str(int((time.time() + 10 * 86400) * 1e9)) + "-" + "a" * 64 + ".gpg"
    names.write_text(future + "\n.partial-aborted\n")
    assert archive.pool_gc(refs, names) == []
    names.write_text("unknown-format\n")
    with pytest.raises(ValueError):
        archive.pool_gc(refs, names)


@pytest.mark.parametrize(
    "preferences",
    [
        ["p/a=bad"],
        ["../a=v2"],
        ["/a=v2"],
        ["p//a=v2"],
        ["p/a=v2", "p/a=legacy"],
        ["=v2"],
        ["p/a\0=v2"],
    ],
)
def test_input_only_preferences_rejected(preferences):
    with pytest.raises(ValueError):
        archive.validate_preferences(preferences)


def test_bad_preferences_leave_db_qdrant_and_epoch_untouched(sandbox):
    _snapshot(sandbox, "archive-host", _NEW)
    result = _run(
        sandbox, host_override="archive-host", extra_args=["--transcript-preference", "p/a=bad"]
    )
    assert result.returncode == 2
    assert not (sandbox["gd"] / "data/genesis.db").exists()
    assert not (sandbox["home"] / ".genesis/locks/transcript-analytics-restore-epoch").exists()
    assert not list(sandbox["backup"].iterdir())


def test_main_discovery_does_not_walk_unrelated_or_subagent_directories(tmp_path, monkeypatch):
    projects, ciphers, scratch = [tmp_path / p for p in ("projects", "ciphers", "scratch")]
    for path in (projects, ciphers, scratch):
        path.mkdir()
    (projects / "main/subagents").mkdir(parents=True)
    (projects / "main/a.jsonl").write_text("main")
    (projects / "main/subagents/agent-a.jsonl").write_text("subagent")
    walked = []
    actual_walk = archive.os.walk

    def guarded_walk(root, **kwargs):
        walked.append(root)
        assert root == projects / "main"
        yield from actual_walk(root, **kwargs)

    monkeypatch.setattr(archive.os, "walk", guarded_walk)
    monkeypatch.setattr(
        archive, "crypt", lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes())
    )
    assert not archive.backup(projects, ciphers, scratch, b"p", "main")
    assert walked == [projects / "main"]
    assert len(list(ciphers.glob("*.gpg"))) == 1
    assert not archive.backup(projects, ciphers, scratch, b"p", "absent")


def test_forced_analytics_aside_is_bounded_unique_and_preserves_existing_store(
    tmp_path, monkeypatch
):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_text("new")
    destination = tmp_path / ("x" * 250)
    destination.mkdir()
    (destination / "old").write_text("old")
    monkeypatch.setattr(
        archive, "crypt", lambda src, dst, *a, **k: dst.write_bytes(src.read_bytes())
    )
    cipher = tmp_path / "data.gpg"
    archive.analytics_archive(source, cipher, scratch, b"p")
    archive.analytics_restore(cipher, destination, scratch, b"p", force=True)
    assert (destination / "data").read_text() == "new"
    aside = list(tmp_path.glob("*.pre-restore-*"))
    assert len(aside) == 1 and len(os.fsencode(aside[0].name)) < 255
    assert (aside[0] / "old").read_text() == "old"
    archive.analytics_restore(cipher, destination, scratch, b"p", force=True)
    assert len(list(tmp_path.glob("*.pre-restore-*"))) == 2


@pytest.mark.parametrize("overlap", ["equal", "ancestor", "descendant"])
def test_dedicated_analytics_is_authoritative_over_generic_extras(backup_env, tmp_path, overlap):
    data = backup_env["home"] / "work/analytics"
    (data / "child").mkdir(parents=True)
    (data / "inventory.json").write_text("{}")
    (data / "derived").mkdir()
    (data / "derived/rebuildable").write_text("excluded")
    (backup_env["gd"] / "config/transcript_analytics.local.yaml").write_text(
        "enabled: true\ndata_dir: " + json.dumps(str(data)) + "\n"
    )
    extra = {"equal": data, "ancestor": data.parent, "descendant": data / "child"}[overlap]
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    result = _run_local(
        backup_env,
        offsite,
        {"GENESIS_REPO_ROOT": str(backup_env["gd"]), "GENESIS_BACKUP_EXTRA_DIRS": str(extra)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    archives = list((backup_env["home"] / "backups/genesis-backups/extra").glob("*.gpg"))
    assert [p.name for p in archives] == ["transcript-analytics-v1.tar.gpg"]
    assert "overlaps dedicated analytics archive" in result.stdout


@pytest.mark.parametrize("backend", ["none", "smb"])
def test_inactive_local_backend_path_does_not_block_analytics(backup_env, tmp_path, backend):
    data = backup_env["home"] / "work/analytics"
    data.mkdir(parents=True)
    (data / "inventory.json").write_text("{}")
    (backup_env["gd"] / "config/transcript_analytics.local.yaml").write_text(
        "enabled: true\ndata_dir: " + json.dumps(str(data)) + "\n"
    )
    offsite = tmp_path / "offsite"
    offsite.mkdir()
    result = _run_local(
        backup_env,
        offsite,
        {
            "GENESIS_REPO_ROOT": str(backup_env["gd"]),
            "GENESIS_BACKUP_TIER2_BACKEND": backend,
            "GENESIS_BACKUP_LOCAL_PATH": str(data),
            "GENESIS_BACKUP_NAS": "//example/share",
            "GENESIS_BACKUP_NAS_USER": "test",
            "GENESIS_BACKUP_NAS_PASS": "test",
        },
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        backup_env["home"] / "backups/genesis-backups/extra/transcript-analytics-v1.tar.gpg"
    ).exists()
    assert "analytics archive unavailable or unsafe" not in result.stdout


def test_gc_missing_entire_pooled_inventory_refuses_deletion(tmp_path):
    captures, backend, scratch = setup_pool(tmp_path)
    assert (
        shell_pool(
            captures, backend, scratch, f'transcript_pool_backup "$captures" {HOST} {SNAPSHOT}'
        ).returncode
        == 0
    )
    orphan = backend / HOST / "transcript-objects" / ("1-" + "f" * 64 + ".gpg")
    orphan.write_bytes(b"obsolete")
    import shutil

    shutil.rmtree(backend / SNAPSHOT / "transcripts")
    result = shell_pool(captures, backend, scratch, f"transcript_pool_gc {HOST} {_NEW}")
    assert result.returncode != 0
    assert orphan.exists()


def test_gc_only_collects_expired_partial_uploads(tmp_path):
    refs = tmp_path / "refs"
    refs.write_text("")
    names = tmp_path / "names"
    old = ".partial-" + str(int(time.time() - 10 * 86400)) + "-1-2-3"
    recent = ".partial-" + str(int(time.time())) + "-1-2-3"
    names.write_text(old + "\n" + recent + "\n")
    assert archive.pool_gc(refs, names) == [old]


@pytest.mark.parametrize(
    "document",
    [
        None,
        [],
        {"version": True, "snapshot": SNAPSHOT, "captures": []},
        {
            "version": 1,
            "snapshot": SNAPSHOT,
            "captures": [["../escape", "1-" + "a" * 64 + ".gpg", 1]],
        },
        {
            "version": 1,
            "snapshot": SNAPSHOT,
            "captures": [[archive.object_name("p/a"), "../object", 1]],
        },
        {
            "version": 1,
            "snapshot": SNAPSHOT,
            "captures": [[archive.object_name("p/a"), "1-" + "a" * 64 + ".gpg", True]],
        },
    ],
)
def test_manifest_schema_rejects_invalid_rows(document):
    with pytest.raises(ValueError):
        archive.pool_rows(document, SNAPSHOT)


@pytest.mark.parametrize("inputs", ["plain", "empty", "encrypted", "mixed"])
def test_restore_set_cli_plain_legacy_needs_no_passphrase(tmp_path, inputs):
    directory = tmp_path / "inputs"
    directory.mkdir()
    root = tmp_path / "restored"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    if inputs in ("plain", "mixed"):
        (directory / "plain.jsonl").write_text("legacy context")
    if inputs in ("encrypted", "mixed"):
        (directory / "encrypted.jsonl.gpg").write_bytes(b"encrypted")
    result = subprocess.run(
        [
            "python3",
            str(ROOT / "scripts/lib/transcript_archive.py"),
            "restore-set",
            str(directory),
            "--root",
            str(root),
            "--scratch",
            str(scratch),
            "--project=p",
        ],
        input=b"",
        capture_output=True,
    )
    assert result.returncode == (1 if inputs in ("encrypted", "mixed") else 0), result.stderr
    if inputs in ("plain", "mixed"):
        assert (root / "p/plain.jsonl").read_text() == "legacy context"
    assert not (root / "p/encrypted.jsonl").exists()


@pytest.mark.parametrize("decrypt", [False, True])
def test_missing_passphrase_rejected_before_any_crypto_io(tmp_path, monkeypatch, decrypt):
    target = tmp_path / "existing"
    target.write_bytes(b"last good")

    def unexpected_subprocess(*args, **kwargs):
        raise AssertionError("crypto command should not run")

    monkeypatch.setattr(archive.subprocess, "run", unexpected_subprocess)
    with pytest.raises(ValueError, match="passphrase"):
        archive.crypt(tmp_path / "source", target, b"", decrypt=decrypt)
    assert target.read_bytes() == b"last good"
