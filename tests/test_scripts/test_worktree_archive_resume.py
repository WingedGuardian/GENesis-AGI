"""Real Git/trash interruption fixtures; never touch the install's stores."""

from __future__ import annotations

import importlib.util
import json
import multiprocessing
import signal
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/worktree_lifecycle.py"
spec = importlib.util.spec_from_file_location("archive_resume", SCRIPT)
wl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = wl
spec.loader.exec_module(wl)


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


@pytest.fixture
def entry(tmp_path, monkeypatch):
    repo, source, trash = (tmp_path / n for n in ("repo", "worktree", "trash"))
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=fixture", "-c", "user.email=fixture@example.invalid",
        "commit", "--allow-empty", "-qm", "initial")
    git(repo, "worktree", "add", "-q", "--detach", str(source))
    (source / "only-copy").write_bytes(b"uncommitted bytes\x00\xff")
    for name, value in (("TRASH_DIR", trash), ("TOMBSTONE_INDEX", tmp_path / "index.jsonl"),
                        ("BOARD_CACHE", tmp_path / "board.json"), ("LOG_DIR", tmp_path / "logs")):
        monkeypatch.setattr(wl, name, value)
    monkeypatch.setattr(wl, "_repo_root", lambda: repo)
    row = wl._list_worktrees(repo)[0]
    compress = wl._compress_entry
    def interrupt(directory, meta):
        wl._archive_path(directory).with_suffix(".gz.part").write_bytes(b"partial")
        raise KeyboardInterrupt
    monkeypatch.setattr(wl, "_compress_entry", interrupt)
    with pytest.raises(KeyboardInterrupt):
        wl._trash_worktree(row, repo, lane="unmerged")
    monkeypatch.setattr(wl, "_compress_entry", compress)
    moved = next(p for p in trash.iterdir() if p.is_dir())
    return repo, source, moved


def run_resume(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--resume-archives", *args])
    return wl.main()


def test_interrupted_directory_is_finished_and_indexed_once(entry, monkeypatch):
    repo, original, moved = entry
    assert not original.exists()
    assert run_resume(monkeypatch) == 0
    archive = wl._archive_path(moved)
    assert archive.is_file()
    assert (moved / "only-copy").read_bytes() == b"uncommitted bytes\x00\xff"
    with tarfile.open(archive) as tf:
        assert tf.extractfile(f"{moved.name}/only-copy").read() == b"uncommitted bytes\x00\xff"
    rows = wl.TOMBSTONE_INDEX.read_bytes()
    assert run_resume(monkeypatch) == 0
    assert wl.TOMBSTONE_INDEX.read_bytes() == rows
    assert len(rows.splitlines()) == 1
    assert wl._archive_anchors(repo)[0]["entry"] == moved.name


def test_published_archive_is_authoritative_and_leftover_is_retained(entry, monkeypatch):
    _, _, moved = entry
    meta = json.loads((moved / ".trash_meta.json").read_text())
    archive = wl._archive_path(moved)
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(moved, arcname=moved.name)
    (moved / "only-copy").unlink()
    (moved / ".trash_meta.json").unlink()
    (moved / "later-addition").write_text("retain this")
    before = archive.read_bytes()
    assert run_resume(monkeypatch) == 0
    assert archive.read_bytes() == before
    assert (moved / "later-addition").read_text() == "retain this"
    assert json.loads(wl.TOMBSTONE_INDEX.read_text())["commit"] == meta["commit"]


@pytest.mark.parametrize("problem", ["dry-run", "bad-metadata", "original-present", "busy", "corrupt-index", "corrupt-archive"])
def test_ambiguous_or_readonly_states_preserve_source(entry, monkeypatch, problem):
    repo, original, moved = entry
    if problem == "bad-metadata":
        (moved / ".trash_meta.json").write_text("{}")
    elif problem == "original-present":
        original.mkdir()
    elif problem == "busy":
        monkeypatch.setattr(wl, "_find_processes_in_dir", lambda p: [123])
    elif problem == "corrupt-index":
        wl.TOMBSTONE_INDEX.write_bytes(b"do not truncate this\n")
    elif problem == "corrupt-archive":
        wl._archive_path(moved).write_bytes(b"damaged published archive")
    before = {p.relative_to(moved): p.read_bytes() for p in moved.rglob("*") if p.is_file()}
    run_resume(monkeypatch, *(["--dry-run"] if problem == "dry-run" else []))
    assert moved.is_dir()
    assert before == {p.relative_to(moved): p.read_bytes() for p in moved.rglob("*") if p.is_file()}
    if problem == "corrupt-index":
        assert wl.TOMBSTONE_INDEX.read_bytes() == b"do not truncate this\n"
    assert wl._archive_anchors(repo)


def test_normal_reaping_resumes_before_classification(entry, monkeypatch):
    def classified(*args, **kwargs):
        assert wl._archive_path(entry[2]).is_file()
        return []
    monkeypatch.setattr(wl, "classify_all", classified)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT)])
    assert wl.main() == 0


def test_a_kill_after_publication_resumes_without_recompressing_leftover(entry, monkeypatch):
    repo, _, moved = entry
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    def interrupted():
        def pause(*args, **kwargs):
            child.send("published")
            signal.pause()
        wl._finish_archive = pause
        wl._resume_archives(repo)
    process = context.Process(target=interrupted)
    process.start()
    try:
        assert parent.poll(10), "child did not reach publication"
        assert parent.recv() == "published"
        process.kill()
        process.join(10)
        assert process.exitcode == -signal.SIGKILL
        assert moved.is_dir() and wl._archive_path(moved).is_file()
        monkeypatch.setattr(wl, "_compress_entry", lambda *a: pytest.fail("published archive was replaced"))
        assert run_resume(monkeypatch) == 0
        assert moved.is_dir()
        assert len(wl.TOMBSTONE_INDEX.read_text().splitlines()) == 1
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        child.close()


def test_an_open_descriptor_outside_the_worktree_cwd_holds_resume(entry, monkeypatch):
    _, _, moved = entry
    with (moved / "only-copy").open("rb"):
        assert run_resume(monkeypatch) == 2
        assert moved.is_dir()


def test_completed_index_entries_are_not_decompressed_again(entry, monkeypatch):
    assert run_resume(monkeypatch) == 0
    monkeypatch.setattr(wl, "_published_identity", lambda *a: pytest.fail("audited completed history"))
    assert run_resume(monkeypatch) == 0


def test_index_publication_interruption_is_idempotent_and_keeps_old_bytes(entry, monkeypatch):
    wl.TOMBSTONE_INDEX.write_bytes(b'{"event":"expired","name":"older"}\r\n')
    original = wl.os.replace
    def publish_then_fail(source, target):
        original(source, target)
        if Path(target) == wl.TOMBSTONE_INDEX:
            raise OSError("interrupted after index publication")
    monkeypatch.setattr(wl.os, "replace", publish_then_fail)
    run_resume(monkeypatch)
    monkeypatch.setattr(wl.os, "replace", original)
    assert run_resume(monkeypatch) == 0
    data = wl.TOMBSTONE_INDEX.read_bytes()
    assert data.startswith(b'{"event":"expired","name":"older"}\r\n')
    assert len(data.splitlines()) == 2
