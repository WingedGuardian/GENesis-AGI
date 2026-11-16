"""Recorded revision is deliberately narrower than loaded-source freshness."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from genesis.observability import commit_identity as identity

A = "a" * 40
B = "b" * 40


@pytest.mark.parametrize(
    "launch,current,expected",
    [
        (A, A, "same_recorded_revision"),
        (A.upper(), A, "same_recorded_revision"),
        (A, B, "different_recorded_revision"),
        (B, A, "different_recorded_revision"),
        (None, B, "unknown"),
        (A, None, "unknown"),
        ("", B, "unknown"),
        (True, B, "unknown"),
        (A[:8], A, "unknown"),
        ("0" * 40, A, "unknown"),
        (A + "\n", A, "unknown"),
        (A, "g" * 40, "unknown"),
        (A, "b" * 64, "unknown"),
        ("a" * 64, "b" * 64, "different_recorded_revision"),
    ],
)
def test_recorded_relation(launch, current, expected):
    assert identity.recorded_revision(launch, current) == expected


def _repo(path: Path) -> str:
    path.mkdir()
    (path / "sample.py").write_text("value = 1\n")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    for args in [
        ["init", "-q"],
        ["add", "sample.py"],
        ["-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
         "-c", "commit.gpgsign=false", "commit", "-qm", path.name],
    ]:
        subprocess.run(["git", "-C", str(path), *args], env=env, check=True,
                       capture_output=True, timeout=10)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"],
                                   env=env, text=True, timeout=10).strip()


def test_runtime_root_not_cwd_or_git_location_override(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    expected = _repo(runtime)
    elsewhere = tmp_path / "elsewhere"
    assert _repo(elsewhere) != expected
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(runtime))
    monkeypatch.setenv("GIT_DIR", str(elsewhere / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(elsewhere))
    monkeypatch.setenv("GIT_INDEX_FILE", str(elsewhere / ".git" / "index"))
    assert identity.read_runtime_head() == expected
    (runtime / "sample.py").write_text("value = 2\n")
    assert identity.read_runtime_head() == expected
    assert identity.recorded_revision(expected, expected) == "same_recorded_revision"


def test_configured_linked_worktree_is_its_own_revision(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    expected = _repo(runtime)
    linked = tmp_path / "linked"
    subprocess.run(["git", "-C", str(runtime), "worktree", "add", "--detach", str(linked)],
                   check=True, capture_output=True, timeout=10)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(linked))
    monkeypatch.chdir(runtime)
    assert identity.read_runtime_head() == expected


def test_runtime_root_subdirectory_is_scope_unknown(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    _repo(runtime)
    subdir = runtime / "subdir"
    subdir.mkdir()
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(subdir))
    assert identity.read_runtime_head() is None


def test_runtime_head_timeout_is_unknown(monkeypatch, tmp_path):
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path))

    def timeout(argv, **kwargs):
        assert kwargs["timeout"] == 1.0
        assert kwargs["stdin"] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(subprocess, "run", timeout)
    assert identity.read_runtime_head() is None


@pytest.mark.parametrize("stdout", ["", "a" * 40, "/wrong/root\n" + A, "x\nnot-a-sha"])
def test_runtime_head_refuses_unverified_output(monkeypatch, tmp_path, stdout):
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout))
    assert identity.read_runtime_head() is None


def test_runtime_root_symlink_loop_is_unknown(tmp_path, monkeypatch):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    monkeypatch.setenv("GENESIS_REPO_ROOT", str(loop))
    assert identity.read_runtime_head() is None
