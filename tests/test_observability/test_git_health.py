"""Tests for git-health detection primitives (F.1).

Exercises the real subprocess/git path against throwaway repos: the exact outage
signatures (zeroed config, nulled packed-refs, missing loose objects) plus the
rootfs read-only probe and the shared-mount verdict writer.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from genesis.observability import git_health as g

_needs_git = pytest.mark.skipif(
    subprocess.run(["which", "git"], capture_output=True).returncode != 0,
    reason="requires git",
)


def _init_repo(path: Path) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init", "-q"], check=True, env=env
    )
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", "https://example.com/x.git"], check=True
    )


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _init_repo(r)
    return r


@_needs_git
class TestCheapCheck:
    @pytest.mark.asyncio
    async def test_healthy_repo_ok(self, repo):
        rep = await g.check_git_cheap(repo)
        assert rep.ok is True
        assert rep.failures == []
        assert rep.details.get("remote_url_present") is True
        assert rep.kind == "cheap"

    @pytest.mark.asyncio
    async def test_zeroed_config_flags_invalid(self, repo):
        (repo / ".git" / "config").write_bytes(b"")
        rep = await g.check_git_cheap(repo)
        assert rep.ok is False
        assert "config_invalid" in rep.failures

    @pytest.mark.asyncio
    async def test_nulled_packed_refs_flagged(self, repo):
        subprocess.run(["git", "-C", str(repo), "pack-refs", "--all"], check=True)
        pr = repo / ".git" / "packed-refs"
        if not pr.exists():  # some git versions need a branch to pack
            pr.write_bytes(b"\x00" * 16)
        else:
            pr.write_bytes(b"\x00" * 16)
        rep = await g.check_git_cheap(repo)
        assert "packed_refs_corrupt" in rep.failures

    @pytest.mark.asyncio
    async def test_missing_git_dir_unresolvable(self, tmp_path):
        # A plain directory that is not a git repo.
        plain = tmp_path / "plain"
        plain.mkdir()
        rep = await g.check_git_cheap(plain)
        assert rep.ok is False
        # not a repo → git rev-parse fails on every probe
        assert "git_dir_unresolvable" in rep.failures


@_needs_git
class TestDeepCheck:
    @pytest.mark.asyncio
    async def test_healthy_repo_ok(self, repo):
        rep = await g.check_git_deep(repo)
        assert rep.ok is True
        assert rep.kind == "deep"

    @pytest.mark.asyncio
    async def test_missing_loose_object_fails_fsck(self, repo):
        # Write a file + commit so there are real blob/tree objects, then delete
        # one loose object → fsck --connectivity-only reports it missing.
        (repo / "f.txt").write_text("hello\n")
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "-C", str(repo), "add", "f.txt"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "add", "-q"], check=True, env=env)
        objdir = repo / ".git" / "objects"
        loose = [
            p for d in objdir.iterdir() if d.is_dir() and len(d.name) == 2 for p in d.iterdir()
        ]
        assert loose, "expected loose objects"
        loose[0].unlink()
        rep = await g.check_git_deep(repo)
        assert rep.ok is False
        assert "fsck_failed" in rep.failures


class TestMountReadonly:
    def test_ro_and_rw_detection(self):
        assert g._mount_is_readonly(Path("/x/y"), "/dev/sda1 /x/y ext4 ro,relatime 0 0") is True
        assert g._mount_is_readonly(Path("/x/y"), "/dev/sda1 /x/y ext4 rw,relatime 0 0") is False

    def test_longest_prefix_wins(self):
        mounts = "/dev/a / ext4 rw 0 0\n/dev/b /x/y ext4 ro 0 0\n"
        assert g._mount_is_readonly(Path("/x/y/z"), mounts) is True
        assert g._mount_is_readonly(Path("/other"), mounts) is False

    def test_unreadable_mounts_no_false_alarm(self):
        # A path whose mount can't be found → not RO (write-probe is authoritative).
        assert g._mount_is_readonly(Path("/x"), "") is False


@_needs_git
class TestVerdictWriter:
    def test_writes_atomic_0600(self, repo, tmp_path):
        rep = g.GitHealthReport(
            ok=False, failures=["rootfs_readonly"], details={}, kind="cheap", checked_at="t"
        )
        shared = tmp_path / "shared"
        shared.mkdir()
        p = g.write_git_health_verdict(rep, shared_dir=shared)
        assert p is not None
        assert p.name == "git_health.json"
        assert p.parent.name == "guardian"
        assert oct(p.stat().st_mode & 0o777) == "0o600"
        import json

        loaded = json.loads(p.read_text())
        assert loaded["ok"] is False
        assert loaded["failures"] == ["rootfs_readonly"]
        assert loaded["version"] == 1

    def test_absent_mount_returns_none(self, tmp_path):
        rep = g.GitHealthReport(ok=True, failures=[], details={}, kind="cheap", checked_at="t")
        assert g.write_git_health_verdict(rep, shared_dir=tmp_path / "nope") is None
