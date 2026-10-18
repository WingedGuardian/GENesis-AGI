"""Publication receipts survive interruption without relabeling frozen inputs."""

import copy
import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.test_scripts.test_backup_dated_snapshots import _run_local
from tests.test_scripts.test_backup_dated_snapshots import backup_env as backup_env

LIB = Path(__file__).parents[2] / "scripts/lib"


@pytest.fixture
def publication(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(LIB))
    spec = importlib.util.spec_from_file_location("publication_test", LIB / "transcript_publication.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scratch, backup, keys = [tmp_path / n for n in ("scratch", "backup", "keys")]
    for p in (scratch, backup, keys):
        p.mkdir(mode=0o700)
    monkeypatch.setenv("GNUPGHOME", str(keys))
    (backup / "data").mkdir()
    (backup / "data/genesis.sql.gpg").write_bytes(b"frozen core ciphertext")
    captures = backup / "transcripts"
    captures.mkdir()
    source = tmp_path / "a.jsonl"
    source.write_bytes(b"original source")
    plain = tmp_path / "source.tar"
    module.archive.capture(source, "p/a.jsonl", plain)
    module.archive.crypt(plain, captures / module.archive.object_name("p/a.jsonl"), b"testpass")
    binding = dict(backend="local", root=str(tmp_path / "offsite"), host="Genesis/test",
                   scope="all", installation=str(backup))
    engine = module.Publication(tmp_path / "spool", scratch, b"testpass", time.monotonic() + 7200)
    doc = engine.create(binding, binding["host"] + "/20261018T000000Z", backup, "", [])
    yield module, engine, doc, binding, backup
    import subprocess
    subprocess.run(["gpgconf", "--homedir", str(keys), "--kill", "gpg-agent"], capture_output=True)


def test_frozen_core_does_not_change_with_producer_and_open_reuses_receipts(publication):
    module, engine, doc, binding, backup = publication
    core = next(r for r in doc["rows"] if r["role"] == "core")
    (backup / "data/genesis.sql.gpg").write_bytes(b"new producer output")
    assert engine.frozen(core).read_bytes() == b"frozen core ciphertext"
    reopened = engine.open(binding, backup / "transcripts")
    assert reopened["snapshot"] == doc["snapshot"]
    assert engine.frozen(reopened["rows"][2]).read_bytes() == b"frozen core ciphertext"


def test_partial_failed_download_checkpoints_and_truncates_only_lost_suffix(publication):
    module, engine, doc, binding, backup = publication
    row = doc["rows"][2]
    row["put"] = False
    expected = engine.frozen(row).read_bytes()
    engine.download(row).write_bytes(expected[:8])
    engine.compare(doc, row, False)
    assert row["verified"] == 8 and not row["receipt"]
    with engine.download(row).open("ab") as out:
        out.write(b"uncheckpointed suffix")
    reopened = engine.open(binding, backup / "transcripts")
    saved = reopened["rows"][2]
    assert saved["verified"] == 8
    assert engine.download(saved).read_bytes() == expected[:8]
    with engine.download(saved).open("ab") as out:
        out.write(expected[8:])
    engine.compare(reopened, saved, True)
    assert saved["receipt"] and saved["verified"] == len(expected)


def test_bad_prefix_and_oversize_never_grant_receipts(publication):
    module, engine, doc, binding, backup = publication
    row = doc["rows"][2]
    for content in (b"bad", engine.frozen(row).read_bytes() + b"extra"):
        engine.download(row).write_bytes(content)
        engine.compare(doc, row, True)
        assert row["verified"] == 0 and not row["receipt"] and row["put"]


@pytest.mark.parametrize("field,value", [("received", True), ("received", -1),
    ("received", 2**63), ("verified", 1), ("generation", False),
    ("receipt", True), ("remote", "../escape"), ("remote", "Genesis/test/other/x"),
    ("id", "../escape"), ("digest", "bad"), ("required", 1), ("role", "unknown")])
def test_malformed_authenticated_row_refused(publication, field, value):
    module, engine, doc, binding, backup = publication
    malformed = copy.deepcopy(doc)
    malformed["rows"][2][field] = value
    with pytest.raises(ValueError):
        engine.validate(malformed, binding)


@pytest.mark.parametrize("field,value", [("scope", "main"), ("root", "other"),
                                         ("host", "Genesis/other")])
def test_changed_binding_retains_original_epoch(publication, field, value):
    module, engine, doc, binding, backup = publication
    other = dict(binding, **{field: value})
    before = (engine.pending / "journal.gpg").read_bytes()
    with pytest.raises(ValueError):
        engine.open(other, backup / "transcripts")
    assert (engine.pending / "journal.gpg").read_bytes() == before


def test_wrong_key_and_changed_index_refuse_without_reset(publication):
    module, engine, doc, binding, backup = publication
    engine.password = b"other-key"
    with pytest.raises((ValueError, module.subprocess.CalledProcessError)):
        engine.open(binding, backup / "transcripts")
    engine.password = b"testpass"
    index = backup / "transcripts" / module.archive._INDEX_NAME
    content, authenticated = module.archive.capture_index(index.parent, engine.scratch, engine.password)
    module.archive.publish_capture_index(index.parent, engine.scratch, engine.password, content)
    with pytest.raises(ValueError, match="index changed"):
        engine.open(binding, index.parent)
    assert engine.pending.exists()


def test_required_receipts_gate_marker_and_original_bytes_gate_retirement(publication):
    module, engine, doc, binding, backup = publication
    with pytest.raises(ValueError, match="receipts incomplete"):
        engine.marker(doc)
    for row in doc["rows"]:
        if row["frozen"] is None:
            engine.freeze_row(row)
        engine.download(row).write_bytes(engine.frozen(row).read_bytes())
        engine.compare(doc, row, True)
    marker = engine.pending / "remote-marker"
    marker.write_bytes(engine.marker(doc) + b"foreign")
    with pytest.raises(ValueError, match="differs"):
        engine.retire(doc, marker)
    marker.write_bytes(engine.marker(doc))
    stamp = doc["snapshot"]
    engine.retire(doc, marker)
    assert not engine.pending.exists()
    assert stamp.endswith("20261018T000000Z")


def test_changed_frozen_file_refuses_reuse(publication):
    module, engine, doc, binding, backup = publication
    engine.frozen(doc["rows"][2]).write_bytes(b"changed")
    with pytest.raises(ValueError, match="frozen.*changed"):
        engine.open(binding, backup / "transcripts")


def test_replaced_download_invalidates_only_its_progress(publication):
    module, engine, doc, binding, backup = publication
    row = doc["rows"][2]
    engine.download(row).write_bytes(engine.frozen(row).read_bytes()[:8])
    engine.compare(doc, row, False)
    replacement = engine.pending / "replacement"
    replacement.write_bytes(b"foreign!")
    os.replace(replacement, engine.download(row))
    reopened = engine.open(binding, backup / "transcripts")
    assert reopened["rows"][2]["verified"] == 0
    assert reopened["snapshot"] == doc["snapshot"]


def test_empty_optional_remote_directory_is_not_an_unknown_file(publication):
    module, engine, doc, binding, backup = publication
    engine.check_listing(doc, doc["snapshot"] + "/qdrant", [""])
    with pytest.raises(ValueError, match="unexpected content"):
        engine.check_listing(doc, doc["snapshot"] + "/qdrant", ["foreign"])


@pytest.mark.parametrize("name", ["a\x85b.local.yaml", "a\u2028b.local.yaml",
                                 "a\u2029b.local.yaml", os.fsdecode(b"a\xffb.local.yaml")])
def test_listing_cli_preserves_filename_bytes(publication, tmp_path, name):
    module, engine, doc, binding, backup = publication
    directory = doc["snapshot"] + "/config"
    doc["rows"].append(dict(remote=directory + "/" + name))
    session, bindings, listing = [tmp_path / n for n in ("session", "binding", "listing")]
    module.write_json(session, doc)
    module.write_json(bindings, binding)
    listing.write_bytes(os.fsencode(name) + b"\n")
    command = [sys.executable, str(LIB / "transcript_publication.py"), "check-list",
               "--spool", str(engine.spool), "--scratch", str(engine.scratch),
               "--session", str(session), "--binding", str(bindings),
               "--deadline", str(engine.deadline), "--directory", directory,
               "--listing", str(listing)]
    result = subprocess.run(command, input=b"testpass", capture_output=True)
    assert result.returncode == 0, result.stderr
    listing.write_bytes(os.fsencode(name) + b"\nunknown\n")
    result = subprocess.run(command, input=b"testpass", capture_output=True)
    assert result.returncode != 0 and b"unexpected content" in result.stderr


def test_postreplace_fence_failure_requires_authentication_and_new_fence(publication, monkeypatch):
    module, engine, doc, binding, backup = publication
    original = (engine.pending / "journal.gpg").read_bytes()
    fence = module.archive._fsync_directory

    def fail_pending(directory):
        if directory == engine.pending:
            raise OSError("synthetic postreplace fence failure")
        fence(directory)

    monkeypatch.setattr(module.archive, "_fsync_directory", fail_pending)
    doc["claimed"] = True
    with pytest.raises(OSError, match="postreplace"):
        engine.checkpoint(doc)
    assert (engine.pending / "journal.gpg").read_bytes() != original
    with pytest.raises(OSError, match="postreplace"):
        engine.open(binding, backup / "transcripts")
    monkeypatch.setattr(module.archive, "_fsync_directory", fence)
    assert engine.open(binding, backup / "transcripts")["claimed"]


def test_empty_failed_download_is_not_an_empty_object_receipt(publication):
    module, engine, doc, binding, backup = publication
    row = doc["rows"][2]
    engine.frozen(row).write_bytes(b"")
    row.update(size=0, frozen=module.regular(engine.frozen(row)))
    engine.download(row).touch()
    engine.compare(doc, row, False)
    assert not row["receipt"]
    engine.compare(doc, row, True)
    assert row["receipt"]


def test_retirement_cleanup_failure_does_not_reactivate_epoch(publication, monkeypatch):
    module, engine, doc, binding, backup = publication
    for row in doc["rows"]:
        if row["frozen"] is None:
            engine.freeze_row(row)
        engine.download(row).write_bytes(engine.frozen(row).read_bytes())
        engine.compare(doc, row, True)
    marker = engine.pending / "ack"
    marker.write_bytes(engine.marker(doc))
    cleanup = module.shutil.rmtree

    def fail_retired(path, *args, **kwargs):
        if Path(path).name.startswith("retired-"):
            raise OSError("synthetic retired cleanup failure")
        return cleanup(path, *args, **kwargs)

    monkeypatch.setattr(module.shutil, "rmtree", fail_retired)
    with pytest.raises(OSError, match="retired cleanup"):
        engine.retire(doc, marker)
    assert not engine.pending.exists() and list(engine.spool.glob("retired-*"))
    monkeypatch.setattr(module.shutil, "rmtree", cleanup)
    engine.reclaim()
    assert not list(engine.spool.glob("retired-*")) and not engine.pending.exists()


def test_failed_preflight_never_creates_remote_snapshot(backup_env, tmp_path):
    remote = tmp_path / "offsite"
    remote.mkdir()
    result = _run_local(backup_env, remote, {"GENESIS_BACKUP_TRANSCRIPT_SCOPE": "invalid"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(remote.iterdir())
    import json
    status = json.loads((backup_env["home"] / ".genesis/backup_status.json").read_text())
    assert not status["success"] and status["tier1_pushed"]


def test_environment_fd_cannot_release_dr_lock(backup_env, tmp_path):
    marker = tmp_path / "lock-at-push"
    script = backup_env["bind"] / "git"
    script.write_text('#!/bin/bash\nif [ "$1" = push ]; then\n'
        f'if flock -n "$HOME/.genesis/locks/backup-restore.lock" -c true; then echo unlocked > "{marker}"; '
        f'else echo locked > "{marker}"; fi\nfi\nexec /usr/bin/git "$@"\n')
    script.chmod(0o700)
    remote = tmp_path / "offsite"
    remote.mkdir()
    result = _run_local(backup_env, remote, {"_ANALYTICS_BACKUP_FD": "200"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert marker.read_text().strip() == "locked"


def test_readbacks_reclaim_only_owned_leftovers(backup_env, tmp_path):
    import hashlib
    scratch = tmp_path / "large-temp"
    binding = hashlib.sha256(str(backup_env["home"] / ".genesis/locks/backup-restore.lock").encode()).hexdigest()[:32]
    owned = scratch / (".genesis-backup-readback-" + binding)
    owned.mkdir(parents=True)
    leftover = owned / ".backend-readback.interrupted"
    leftover.write_bytes(b"owned interrupted copy")
    foreign = scratch / "foreign"
    foreign.write_bytes(b"unrelated data")
    remote = tmp_path / "offsite"
    remote.mkdir()
    result = _run_local(backup_env, remote, {"GENESIS_BACKUP_TMPDIR": str(scratch)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert not leftover.exists() and not list(owned.glob(".backend-readback.*"))
    assert foreign.read_bytes() == b"unrelated data"


@pytest.mark.parametrize("trigger", ["timer", "update"])
def test_two_runs_finish_frozen_snapshot_and_keep_current_sql(backup_env, tmp_path, trigger):
    import json
    remote = tmp_path / "offsite"
    remote.mkdir()
    source = backup_env["home"] / ".claude/projects" / str(backup_env["gd"]).replace("/", "-") / "session.jsonl"
    source.write_bytes(b"original transcript")
    wrapper = backup_env["bind"] / "cp"
    wrapper.write_text('#!/bin/bash\ncase "${2:-}" in */transcript-objects/.partial-*) '
                       '[ "${PUBLICATION_TEST_FAIL_POOL:-}" = 1 ] && exit 1;; esac\nexec /usr/bin/cp "$@"\n')
    wrapper.chmod(0o700)
    first = _run_local(backup_env, remote, {"PUBLICATION_TEST_FAIL_POOL": "1"})
    assert first.returncode == 0, first.stdout + first.stderr
    status_file = backup_env["home"] / ".genesis/backup_status.json"
    original = json.loads(status_file.read_text())
    assert original["tier2_status"] == "partial" and not list(remote.rglob("COMPLETE"))
    source.write_bytes(b"newer deferred transcript")
    subprocess.run(["sqlite3", str(backup_env["gd"] / "data/genesis.db"), "UPDATE t SET x=2;"], check=True)
    second = _run_local(backup_env, remote, {"GENESIS_BACKUP_TRIGGER": trigger})
    assert second.returncode == 0, second.stdout + second.stderr
    status = json.loads(status_file.read_text())
    assert status["tier2_status"] == "ok" and status["snapshot_id"] == original["snapshot_id"]
    assert status["sqlite_backup_verified"] and len(list(remote.rglob("COMPLETE"))) == 1
    local = backup_env["home"] / "backups/genesis-backups"
    decrypt = ["gpg", "--batch", "--yes", "--pinentry-mode", "loopback", "--passphrase-fd", "0", "--decrypt"]
    env = dict(os.environ, GNUPGHOME=str(backup_env["home"] / ".gnupg"))
    current = subprocess.run([*decrypt, str(local / "data/genesis.sql.gpg")], input=b"testpass", env=env, check=True, capture_output=True).stdout
    frozen = subprocess.run([*decrypt, str(next(remote.rglob("genesis.sql.gpg")))], input=b"testpass", env=env, check=True, capture_output=True).stdout
    assert b"VALUES(2)" in current and b"VALUES(1)" in frozen
    assert source.read_bytes() == b"newer deferred transcript"
    assert not (local.parent / ".genesis-backup-publication/pending").exists()
