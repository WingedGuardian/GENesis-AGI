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
    async def test_corrupt_config_flags_invalid(self, repo):
        # Null-filled config (the incident signature) → `git config --list` fatal.
        (repo / ".git" / "config").write_bytes(b"\x00" * 64)
        rep = await g.check_git_cheap(repo)
        assert rep.ok is False
        assert "config_invalid" in rep.failures

    @pytest.mark.asyncio
    async def test_missing_origin_not_flagged(self, repo):
        # A valid local clone with NO origin remote is healthy for local recovery
        # (git revert is local); must NOT flag config_invalid, only note absence.
        subprocess.run(["git", "-C", str(repo), "remote", "remove", "origin"], check=True)
        rep = await g.check_git_cheap(repo)
        assert "config_invalid" not in rep.failures
        assert rep.ok is True
        assert rep.details.get("remote_url_present") is False

    @pytest.mark.asyncio
    async def test_empty_config_not_flagged(self, repo):
        # A truncated-to-empty config parses fine (git falls back to global) —
        # recoverable, so not flagged.
        (repo / ".git" / "config").write_bytes(b"")
        rep = await g.check_git_cheap(repo)
        assert "config_invalid" not in rep.failures

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
    async def test_empty_packed_refs_is_healthy(self, repo):
        # A 0-byte packed-refs is a LEGITIMATE state (refs all loose) — it must
        # NOT flag corrupt, only null-BYTE content does. The healthy repo's refs
        # stay resolvable, so the overall report is ok.
        (repo / ".git" / "packed-refs").write_bytes(b"")
        rep = await g.check_git_cheap(repo)
        assert "packed_refs_corrupt" not in rep.failures
        assert rep.ok is True

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

    @staticmethod
    def _commit_file(repo, name="f.txt", content="hello\n"):
        (repo / name).write_text(content)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
        subprocess.run(["git", "-C", str(repo), "add", name], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-m", "add", "-q"], check=True, env=env)

    @pytest.mark.asyncio
    async def test_missing_loose_object_fails_fsck(self, repo):
        # Write a file + commit so there are real blob/tree objects, then delete
        # one loose object → fsck reports it missing.
        self._commit_file(repo)
        objdir = repo / ".git" / "objects"
        loose = [
            p for d in objdir.iterdir() if d.is_dir() and len(d.name) == 2 for p in d.iterdir()
        ]
        assert loose, "expected loose objects"
        loose[0].unlink()
        rep = await g.check_git_deep(repo)
        assert rep.ok is False
        assert "fsck_failed" in rep.failures

    @pytest.mark.asyncio
    async def test_zeroed_loose_object_fails_fsck(self, repo):
        # The exact outage pattern: a reachable loose blob is zero-filled but still
        # PRESENT. `git fsck --connectivity-only` (the old impl) passes this — it
        # never rehashes content — so the deep check MUST use `--full`, which
        # recomputes SHA-1 and flags the corruption. This test fails under the old
        # flag and passes under the fix (P1, #1010 Codex re-review).
        self._commit_file(repo)
        blob = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD:f.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        obj = repo / ".git" / "objects" / blob[:2] / blob[2:]
        size = obj.stat().st_size
        obj.chmod(0o644)
        obj.write_bytes(b"\x00" * size)  # present, right size, all-NUL content
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
        assert loaded["version"] == 2

    def test_absent_mount_returns_none(self, tmp_path):
        rep = g.GitHealthReport(ok=True, failures=[], details={}, kind="cheap", checked_at="t")
        assert g.write_git_health_verdict(rep, shared_dir=tmp_path / "nope") is None

    def test_passing_cheap_tick_does_not_erase_failed_deep_verdict(self, tmp_path):
        """P2 (#1010 Codex re-review): deep-only corruption (a zeroed reachable
        blob) is invisible to the cheap probe. A subsequent passing cheap tick must
        NOT flip the shared verdict back to healthy — the deep failure persists in
        its own slot and stays in the top-level union the guardian reads."""
        import json

        shared = tmp_path / "shared"
        shared.mkdir()
        # Daily deep run finds corruption.
        deep_fail = g.GitHealthReport(
            ok=False, failures=["fsck_failed"], details={}, kind="deep", checked_at="t1"
        )
        g.write_git_health_verdict(deep_fail, shared_dir=shared)
        # Next cheap tick is healthy (it can't see the deep corruption).
        cheap_ok = g.GitHealthReport(
            ok=True, failures=[], details={}, kind="cheap", checked_at="t2"
        )
        p = g.write_git_health_verdict(cheap_ok, shared_dir=shared)

        loaded = json.loads(p.read_text())
        assert loaded["ok"] is False, "deep failure must survive a passing cheap tick"
        assert "fsck_failed" in loaded["failures"]
        assert loaded["deep"]["ok"] is False
        assert loaded["cheap"]["ok"] is True

    def test_cheap_failure_is_live_and_clears_on_next_ok(self, tmp_path):
        """The cheap slot is LIVE: a fixed cheap failure clears on the next tick,
        while any recorded deep result is preserved untouched."""
        import json

        shared = tmp_path / "shared"
        shared.mkdir()
        g.write_git_health_verdict(
            g.GitHealthReport(ok=True, failures=[], details={}, kind="deep", checked_at="d"),
            shared_dir=shared,
        )
        g.write_git_health_verdict(
            g.GitHealthReport(
                ok=False, failures=["rootfs_readonly"], details={}, kind="cheap", checked_at="c1"
            ),
            shared_dir=shared,
        )
        p = g.write_git_health_verdict(
            g.GitHealthReport(ok=True, failures=[], details={}, kind="cheap", checked_at="c2"),
            shared_dir=shared,
        )
        loaded = json.loads(p.read_text())
        assert loaded["ok"] is True
        assert loaded["failures"] == []
        assert loaded["deep"]["ok"] is True  # preserved

    def test_legacy_v1_deep_failure_survives_migration(self, tmp_path):
        """P2 (#1010 Codex re-review): a pre-upgrade v1 verdict (top-level
        kind/failures, no slots) written by the OLD deep job must be seeded into
        the deep slot, so the first v2 cheap-ok tick doesn't drop it and reopen the
        24h blind spot the two-slot format closes."""
        import json

        shared = tmp_path / "shared"
        (shared / "guardian").mkdir(parents=True)
        # Hand-write a legacy v1 deep failure (the pre-migration schema).
        (shared / "guardian" / "git_health.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "ok": False,
                    "failures": ["fsck_failed"],
                    "kind": "deep",
                    "checked_at": "old",
                    "details": {},
                }
            )
        )
        # First v2 write is a passing cheap tick.
        p = g.write_git_health_verdict(
            g.GitHealthReport(ok=True, failures=[], details={}, kind="cheap", checked_at="new"),
            shared_dir=shared,
        )
        loaded = json.loads(p.read_text())
        assert loaded["ok"] is False, "legacy deep failure must survive v1→v2 migration"
        assert "fsck_failed" in loaded["failures"]
        assert loaded["deep"]["ok"] is False

    def test_top_level_failures_are_union_of_both_slots(self, tmp_path):
        import json

        shared = tmp_path / "shared"
        shared.mkdir()
        g.write_git_health_verdict(
            g.GitHealthReport(
                ok=False, failures=["fsck_failed"], details={}, kind="deep", checked_at="d"
            ),
            shared_dir=shared,
        )
        p = g.write_git_health_verdict(
            g.GitHealthReport(
                ok=False, failures=["rootfs_readonly"], details={}, kind="cheap", checked_at="c"
            ),
            shared_dir=shared,
        )
        loaded = json.loads(p.read_text())
        assert set(loaded["failures"]) == {"fsck_failed", "rootfs_readonly"}
        assert loaded["ok"] is False
