"""Retention lifecycle and complete-admission refusal, with isolated synthetic HOME."""
import hashlib
import json
import os
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from genesis.eval.qualification import __main__ as cli
from genesis.eval.qualification import preflight, sources
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_preflight_fixtures import admitted


@pytest.fixture
def retained(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    original = tmp_path / "temporary source ' $ with ünicode"
    original.write_bytes(b"original frozen evidence")
    mapping = {str(original): hashlib.sha256(original.read_bytes()).hexdigest()}
    primary = sources.archive(mapping, "generation ü ' $")
    restored = sources.restore_store() / "restored"
    restored.parent.mkdir(mode=0o700)
    shutil.copytree(primary, restored)
    return original, mapping, {
        "primary_generation": str(primary), "restored_backup_generation": str(restored),
        "backup_evidence": "Synthetic local-copy control, not an independent backup claim",
    }


def test_retained_bytes_survive_loss_of_original_path(retained):
    original, mapping, proof = retained
    original.unlink()
    assert sources.retained(mapping, proof) == mapping
    cases = {"synthetic": [{"source": {"path": str(original), "sha256": next(iter(mapping.values()))}}]}
    assert preflight.source_evidence(cases, mapping, retention=proof) == mapping
    cases["synthetic"][0]["source"]["path"] = "unbound origin"
    with pytest.raises(Incomplete, match="path differs"):
        preflight.source_evidence(cases, mapping, retention=proof)


@pytest.mark.parametrize("side", ["primary_generation", "restored_backup_generation"])
@pytest.mark.parametrize("damage", ["missing", "corrupt", "extra", "manifest", "blob_link",
                                    "manifest_link", "permissions", "directory_link"])
def test_every_required_archive_is_checked(retained, tmp_path, side, damage):
    _, mapping, proof = retained
    root = Path(proof[side])
    blob = root / "blobs" / next(iter(mapping.values()))
    if damage == "missing":
        blob.unlink()
    elif damage == "corrupt":
        blob.write_bytes(b"changed")
    elif damage == "extra":
        (root / "extra").write_text("foreign")
    elif damage == "manifest":
        (root / "manifest.json").write_text("{}")
    elif damage in ("blob_link", "manifest_link"):
        target = blob if damage == "blob_link" else root / "manifest.json"
        other = tmp_path / "link-target"
        target.rename(other)
        target.symlink_to(other)
    elif damage == "directory_link":
        other = tmp_path / "directory-target"
        root.rename(other)
        root.symlink_to(other, target_is_directory=True)
    else:
        blob.chmod(0o644)
    with pytest.raises((Incomplete, OSError)):
        sources.retained(mapping, proof)


@pytest.mark.parametrize("change", ["same", "inside", "hardlink", "outside_store", "no_evidence",
                                    "restore_outside", "restore_parent_link", "parent_permissions"])
def test_copy_aliases_and_missing_restore_assertions_refuse(retained, tmp_path, change):
    _, mapping, proof = retained
    primary, restored = (Path(proof[k]) for k in ("primary_generation", "restored_backup_generation"))
    if change == "same":
        proof["restored_backup_generation"] = str(primary)
    elif change == "inside":
        proof["restored_backup_generation"] = str(primary / "inside")
    elif change == "hardlink":
        blob = next(iter(mapping.values()))
        (restored / "blobs" / blob).unlink()
        os.link(primary / "blobs" / blob, restored / "blobs" / blob)
    elif change == "outside_store":
        moved = tmp_path / "outside"
        primary.rename(moved)
        proof["primary_generation"] = str(moved)
    elif change == "restore_outside":
        moved = tmp_path / "ordinary-checkout-name"
        restored.rename(moved)
        proof["restored_backup_generation"] = str(moved)
    elif change == "restore_parent_link":
        moved = tmp_path / "disposable-checkout"
        restored.parent.rename(moved)
        restored.parent.symlink_to(moved, target_is_directory=True)
    elif change == "parent_permissions":
        restored.parent.chmod(0o777)
    else:
        proof["backup_evidence"] = " "
    with pytest.raises((Incomplete, OSError)):
        sources.retained(mapping, proof)


def test_alias_to_disposable_restore_does_not_pass_the_original_lexical_gap(retained, tmp_path):
    _, mapping, proof = retained
    restored = Path(proof["restored_backup_generation"])
    checkout = tmp_path / "worktrees"
    restored.parent.rename(checkout)
    alias = tmp_path / "restore-location"
    alias.symlink_to(checkout, target_is_directory=True)
    proof["restored_backup_generation"] = str(alias / restored.name)
    with pytest.raises(Incomplete, match="durable"):
        sources.retained(mapping, proof)


def test_equal_hash_aliases_are_each_verified_and_existing_generation_untouched(retained, tmp_path):
    original, mapping, proof = retained
    alias = tmp_path / "another logical origin"
    alias.write_bytes(original.read_bytes())
    mapping[str(alias)] = next(iter(mapping.values()))
    first = sources.archive(mapping, "equal-aliases")
    assert len(list((first / "blobs").iterdir())) == 1
    before = (first / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        sources.archive(mapping, "equal-aliases")
    assert (first / "manifest.json").read_bytes() == before
    alias.write_bytes(b"not the frozen bytes")
    with pytest.raises(Incomplete, match="differ"):
        sources.archive(mapping, "bad-alias")
    assert not (sources.store() / "bad-alias/manifest.json").exists()


def test_source_mutation_during_copy_never_publishes_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    original = tmp_path / "source"
    original.write_bytes(b"frozen")
    mapping = {str(original): hashlib.sha256(original.read_bytes()).hexdigest()}
    actual_sync = os.fsync

    def mutate(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            original.write_bytes(b"changed while copying")
        actual_sync(fd)

    monkeypatch.setattr(sources.os, "fsync", mutate)
    with pytest.raises(Incomplete, match="changed during"):
        sources.archive(mapping, "mutation")
    assert not (sources.store() / "mutation/manifest.json").exists()


@pytest.mark.parametrize("failure", ["missing", "hash", "alias", "read", "mutation"])
def test_later_source_failure_preserves_checkpointed_evidence(tmp_path, monkeypatch, failure):
    monkeypatch.setenv("HOME", str(tmp_path))
    first, second = tmp_path / "first", tmp_path / "second"
    first.write_bytes(b"first frozen source")
    second_bytes = b"second frozen source"
    second.write_bytes(second_bytes)
    first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
    second_hash = hashlib.sha256(second_bytes).hexdigest()
    old = sources.archive({str(first): first_hash}, "existing")

    def snapshot():
        return {str(p.relative_to(old)): (p.stat().st_mode, p.stat().st_mtime_ns,
                p.read_bytes() if p.is_file() else None) for p in [old, *old.rglob("*")]}

    preserved = snapshot()
    if failure == "missing":
        second.unlink()
    expected = "0" * 64 if failure == "hash" else first_hash if failure == "alias" else second_hash
    mapping = {str(first): first_hash, str(second): expected}
    real_sync, real_read = os.fsync, sources._read
    syncs, before_second = [], []
    interrupted = False

    def measured(fd):
        syncs.append((Path(os.readlink(f"/proc/self/fd/{fd}")), interrupted))
        real_sync(fd)

    @contextmanager
    def read(path, *, private):
        nonlocal interrupted
        if path == second:
            before_second.extend(syncs)
        with real_read(path, private=private) as stream:
            if path == second and failure == "read":
                class Interrupted:
                    def read(self, size):
                        nonlocal interrupted
                        if interrupted:
                            raise OSError("synthetic interrupted source read")
                        interrupted = True
                        return stream.read(4)

                yield Interrupted()
            else:
                yield stream
                if path == second and failure == "mutation":
                    second.write_bytes(b"synthetic source mutation")

    monkeypatch.setattr(sources.os, "fsync", measured)
    monkeypatch.setattr(sources, "_read", read)
    with pytest.raises((OSError, Incomplete)):
        sources.archive(mapping, "aborted")
    generation = sources.store() / "aborted"
    blobs = generation / "blobs"
    assert (blobs / first_hash).read_bytes() == first.read_bytes()
    assert not (generation / "manifest.json").exists()
    checkpointed = {path for path, _ in before_second}
    assert {generation, blobs, blobs / first_hash} <= checkpointed
    if failure == "read":
        assert (blobs / second_hash).read_bytes() == second_bytes[:4]
        assert (blobs / second_hash, True) in syncs
    assert snapshot() == preserved


@pytest.mark.parametrize("generation", ["", ".", "..", "../escape", "/absolute", "a/b", "a\x00b"])
def test_generation_never_escapes_store(tmp_path, monkeypatch, generation):
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(Incomplete):
        sources.archive({str(tmp_path / "missing"): "0" * 64}, generation)
    assert not sources.store().exists()


@pytest.mark.parametrize("kind", ["missing", "file"])
def test_archive_requires_an_existing_home_boundary(tmp_path, monkeypatch, kind):
    home = tmp_path / "home"
    if kind == "file":
        home.write_bytes(b"not a directory")
    monkeypatch.setenv("HOME", str(home))
    with pytest.raises(Incomplete, match="existing HOME"):
        sources.archive({str(tmp_path / "unused"): "0" * 64}, "refused")
    if kind == "missing":
        assert not home.exists()
    else:
        assert home.read_bytes() == b"not a directory"


def test_each_sync_failure_reports_failure_and_retains_evidence(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.write_bytes(b"stable")
    mapping = {str(original): hashlib.sha256(original.read_bytes()).hexdigest()}
    actual_sync, calls = os.fsync, []

    def measured(fd):
        calls.append(fd)
        actual_sync(fd)

    home = tmp_path / "baseline"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sources.os, "fsync", measured)
    sources.archive(mapping, "sync")
    total = len(calls)
    assert total > 5
    readable_after_failure = 0
    for boundary in range(1, total + 1):
        home = tmp_path / f"failure-{boundary}"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        calls.clear()

        def failing(fd, selected=boundary):
            calls.append(fd)
            if len(calls) == selected:
                raise OSError("synthetic sync failure")
            actual_sync(fd)

        monkeypatch.setattr(sources.os, "fsync", failing)
        with pytest.raises(OSError, match="synthetic sync failure"):
            sources.archive(mapping, "sync")
        root = sources.store() / "sync"
        if (root / "manifest.json").exists():
            sources._verify(root, mapping)
            readable_after_failure += 1  # Completeness is not successful durable publication.
        # A fresh generation after any failure must establish the complete
        # ancestor chain, including directories left by the failed invocation.
        def snapshot(directory):
            return {str(p.relative_to(directory)): (p.stat().st_mode, p.stat().st_mtime_ns,
                    p.read_bytes() if p.is_file() else None)
                    for p in [directory, *directory.rglob("*")]} if directory.exists() else {}

        preserved, retry_calls = snapshot(root), []
        retry = sources.store() / "retry"

        def retry_sync(fd, recorded=retry_calls, destination=retry):
            recorded.append((Path(os.readlink(f"/proc/self/fd/{fd}")), destination.exists()))
            actual_sync(fd)

        monkeypatch.setattr(sources.os, "fsync", retry_sync)
        assert sources.archive(mapping, "retry") == retry
        before_creation = [path for path, exists in retry_calls if not exists]
        assert before_creation[-3:] == [sources.store(), sources.store().parent, home]
        assert snapshot(root) == preserved
        sources._verify(retry, mapping)
        assert original.read_bytes() == b"stable"
    assert readable_after_failure > 0


@pytest.mark.parametrize("damage", ["absent", "restored_corrupt", "primary_missing"])
async def test_complete_admission_refuses_retention_failure_before_render(tmp_path, monkeypatch, damage):
    cases, options = admitted(tmp_path, monkeypatch)
    proof = options["reference_approval"]["source_retention"]
    if damage == "absent":
        options["reference_approval"].pop("source_retention")
    else:
        path = Path(proof["restored_backup_generation" if damage == "restored_corrupt"
                          else "primary_generation"]) / "blobs" / next(iter(options["source_files"].values()))
        path.write_bytes(b"corrupt") if damage == "restored_corrupt" else path.unlink()

    async def forbidden(*_, **kwargs):
        pytest.fail("invalid retained evidence reached rendering")

    monkeypatch.setattr(preflight, "prepare", forbidden)
    with pytest.raises((Incomplete, OSError)):
        await preflight.check(cases, tmp_path, **options)


def test_actual_cli_archives_without_source_or_approval_mutation(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    original = tmp_path / "temporary input"
    original.write_bytes(b"retained")
    spec = tmp_path / "sources.json"
    spec.write_text(json.dumps({str(original): hashlib.sha256(original.read_bytes()).hexdigest()}))
    before = original.read_bytes(), spec.read_bytes()
    argv = ["archive-sources", "--source-files", str(spec), "--generation", "cli"]
    assert cli.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["scope"] == "source retention only; no approval or backup claim"
    assert cli.main(argv) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"
    assert before == (original.read_bytes(), spec.read_bytes())
