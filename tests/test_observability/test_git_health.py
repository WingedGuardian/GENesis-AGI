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


@pytest.fixture(autouse=True)
def recheck_sleeps(monkeypatch):
    """A failing deep run waits ``_FSCK_RECHECK_DELAY_S`` before its re-check; record
    the waits instead of sleeping (real-repo tests below assert on them)."""
    calls: list[float] = []

    async def _fake_sleep(delay):
        calls.append(delay)

    monkeypatch.setattr(g, "_asleep", _fake_sleep)
    return calls


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
    async def test_healthy_repo_ok(self, repo, recheck_sleeps):
        rep = await g.check_git_deep(repo)
        assert rep.ok is True
        assert rep.kind == "deep"
        assert recheck_sleeps == []
        assert "fsck_transient" not in rep.details

    @pytest.mark.asyncio
    async def test_dangling_objects_are_not_failures_or_evidence(self, repo, recheck_sleeps):
        # An unreachable blob is "dangling": benign, exit 0, and (with
        # --no-dangling) not even printed.
        subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input="orphan\n",
            check=True,
            capture_output=True,
            text=True,
        )
        precondition = subprocess.run(
            ["git", "-C", str(repo), "fsck", "--no-progress", "--full", "--no-reflogs"],
            capture_output=True,
            text=True,
        )
        assert "dangling blob" in precondition.stdout, "precondition: a dangling object exists"
        rc, out, err = g._run_fsck(repo)
        assert rc == 0
        assert "dangling" not in out + err
        rep = await g.check_git_deep(repo)
        assert rep.ok is True and recheck_sleeps == []

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
    async def test_missing_loose_object_fails_fsck(self, repo, recheck_sleeps):
        # Write a file + commit, then delete the file's BLOB → fsck reports it
        # missing. Named, not the first loose object found: a deleted commit is
        # reported as an "invalid sha1 pointer" on the ref instead (CI, #3137).
        self._commit_file(repo)
        blob = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD:f.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()
        rep = await g.check_git_deep(repo)
        assert rep.ok is False
        assert "fsck_failed" in rep.failures
        # Real corruption persists: the re-check reproduces it, after one wait.
        assert recheck_sleeps == [g._FSCK_RECHECK_DELAY_S]
        assert rep.details["fsck_reproduced"] is True
        assert "missing" in rep.details["fsck_stderr"]

    @pytest.mark.asyncio
    async def test_zeroed_loose_object_fails_fsck(self, repo, recheck_sleeps):
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
        assert recheck_sleeps == [g._FSCK_RECHECK_DELAY_S]
        assert rep.details["fsck_reproduced"] is True
        assert blob[:2] in rep.details["fsck_stderr"]

    @pytest.mark.asyncio
    async def test_invalid_reflog_entry_not_flagged(self, repo):
        # Routine branch/worktree churn + gc can leave a branch reflog
        # referencing a pruned commit; `git fsck --full` then prints "invalid
        # reflog entry" and exits non-zero. That is NOT object corruption and is
        # irrelevant to this check's object-integrity purpose (and to REVERT_CODE,
        # which the guardian gates on a live cheap probe, not this verdict), so
        # the deep scan runs with --no-reflogs and MUST NOT flag it. Root cause of
        # the recurring false "objects corrupt" CRITICAL (verified 2026-08-25).
        self._commit_file(repo)
        subprocess.run(["git", "-C", str(repo), "branch", "feat"], check=True)
        reflog = repo / ".git" / "logs" / "refs" / "heads" / "feat"
        reflog.parent.mkdir(parents=True, exist_ok=True)
        reflog.write_text(
            "0000000000000000000000000000000000000000 "
            "deadbeef00000000000000000000000000000000 t <t@t> 1600000000 +0000\tbogus\n"
        )
        # Lock WHY this test is meaningful: a reflog-CHECKING fsck DOES fail on this
        # same repo, so the test goes red if --no-reflogs is ever dropped.
        with_reflogs = subprocess.run(
            ["git", "-C", str(repo), "fsck", "--no-progress", "--full"],
            capture_output=True,
            text=True,
        )
        assert with_reflogs.returncode != 0 and "invalid reflog entry" in with_reflogs.stderr, (
            "precondition: the injected entry must make a reflog-checking fsck fail"
        )
        rep = await g.check_git_deep(repo)
        assert rep.ok is True, f"reflog noise must not be flagged; failures={rep.failures}"
        assert "fsck_failed" not in rep.failures


def _fsck_runs(monkeypatch, *results):
    """Replace fsck with scripted (rc, out, err) results, in order."""
    queue = list(results)
    calls: list[Path] = []

    def _fake(repo):
        calls.append(repo)
        return queue.pop(0)

    monkeypatch.setattr(g, "_run_fsck", _fake)
    return calls


_DANGLING = "\n".join(f"dangling blob {i:040x}" for i in range(3000))
_MISSING = "missing blob 1234567890abcdef1234567890abcdef12345678"


class TestDeepRecheck:
    """#2745: evidence is the failing lines, and one failure is re-checked."""

    @pytest.mark.asyncio
    async def test_reproduced_failure_reports_rerun_evidence(self, monkeypatch, recheck_sleeps):
        calls = _fsck_runs(
            monkeypatch,
            (2, _DANGLING + "\nmissing blob aaaa", ""),
            (2, _DANGLING + "\n" + _MISSING, "error: sha1 mismatch for .git/objects/12/34"),
        )
        rep = await g.check_git_deep(Path("/r"))
        assert len(calls) == 2 and recheck_sleeps == [g._FSCK_RECHECK_DELAY_S]
        assert rep.failures == ["fsck_failed"]
        assert rep.details["fsck_reproduced"] is True
        assert rep.details["fsck_rc"] == 2
        ev = rep.details["fsck_stderr"]
        assert "dangling" not in ev
        assert ev.splitlines() == ["error: sha1 mismatch for .git/objects/12/34", _MISSING]

    @pytest.mark.asyncio
    async def test_transient_failure_passes_with_record(self, monkeypatch, recheck_sleeps, caplog):
        _fsck_runs(monkeypatch, (2, _MISSING, ""), (0, "", ""))
        with caplog.at_level("WARNING", logger=g.__name__):
            rep = await g.check_git_deep(Path("/r"))
        assert rep.ok is True and rep.failures == []
        assert rep.details["fsck_transient"] == {
            "rc": 2,
            "lines": _MISSING,
            "delay_s": g._FSCK_RECHECK_DELAY_S,
        }
        assert "passed on re-check" in caplog.text

    @pytest.mark.asyncio
    async def test_recheck_timeout_stays_a_failure(self, monkeypatch, recheck_sleeps):
        _fsck_runs(monkeypatch, (2, _MISSING, ""), (-1, "", "timeout"))
        rep = await g.check_git_deep(Path("/r"))
        assert rep.failures == ["fsck_failed"]
        assert rep.details["fsck_recheck"] == "timeout"
        assert rep.details["fsck_stderr"] == _MISSING  # first run's evidence
        assert "fsck_reproduced" not in rep.details

    @pytest.mark.parametrize("rc2", [-2, -9])
    @pytest.mark.asyncio
    async def test_recheck_that_could_not_run_is_not_reproduced(
        self, monkeypatch, recheck_sleeps, rc2
    ):
        # A re-check killed by a signal (e.g. a server stop) or unable to start
        # proves nothing: keep run 1's evidence, never claim "reproduced".
        _fsck_runs(monkeypatch, (2, _MISSING, ""), (rc2, "", "boom"))
        rep = await g.check_git_deep(Path("/r"))
        assert rep.failures == ["fsck_failed"]
        assert rep.details["fsck_recheck"] == f"incomplete (rc={rc2})"
        assert rep.details["fsck_stderr"] == _MISSING
        assert rep.details["fsck_rc"] == 2
        assert "fsck_reproduced" not in rep.details

    @pytest.mark.asyncio
    async def test_first_run_killed_is_rechecked(self, monkeypatch, recheck_sleeps):
        calls = _fsck_runs(monkeypatch, (-9, "", ""), (0, "", ""))
        rep = await g.check_git_deep(Path("/r"))
        assert rep.ok is True and len(calls) == 2
        assert rep.details["fsck_transient"]["rc"] == -9

    @pytest.mark.asyncio
    async def test_sigterm_on_either_run_aborts_with_no_verdict(self, monkeypatch, recheck_sleeps):
        # systemd SIGTERMs the whole cgroup on a service stop, git included: a scan
        # cut off that way proves nothing and must not page on the next start.
        import asyncio

        calls = _fsck_runs(monkeypatch, (-15, "", ""))
        with pytest.raises(asyncio.CancelledError):
            await g.check_git_deep(Path("/r"))
        assert len(calls) == 1 and recheck_sleeps == []
        _fsck_runs(monkeypatch, (2, _MISSING, ""), (-15, "", ""))
        with pytest.raises(asyncio.CancelledError):
            await g.check_git_deep(Path("/r"))

    @pytest.mark.asyncio
    async def test_first_run_timeout_is_not_rechecked(self, monkeypatch, recheck_sleeps):
        calls = _fsck_runs(monkeypatch, (-1, "", "timeout"))
        rep = await g.check_git_deep(Path("/r"))
        assert rep.failures == ["fsck_timeout"]
        assert len(calls) == 1 and recheck_sleeps == []

    @pytest.mark.asyncio
    async def test_exec_failure_is_not_rechecked(self, monkeypatch, recheck_sleeps):
        calls = _fsck_runs(monkeypatch, (-2, "", "No such file or directory: git"))
        rep = await g.check_git_deep(Path("/r"))
        assert rep.failures == ["fsck_failed"]
        assert rep.details["fsck_rc"] == -2
        assert "No such file" in rep.details["fsck_stderr"]
        assert len(calls) == 1 and recheck_sleeps == []

    @pytest.mark.asyncio
    async def test_cancel_during_wait_propagates(self, monkeypatch):
        import asyncio

        async def _cancelled(_delay):
            raise asyncio.CancelledError

        monkeypatch.setattr(g, "_asleep", _cancelled)
        calls = _fsck_runs(monkeypatch, (2, _MISSING, ""))
        with pytest.raises(asyncio.CancelledError):
            await g.check_git_deep(Path("/r"))
        assert len(calls) == 1


@_needs_git
class TestRaceLookup:
    """A re-check that fails can race too; it counts as transient only when every
    line is ``missing <type> <sha>`` and every such object exists (real git)."""

    @staticmethod
    def _blob(repo, text="present\n"):
        return subprocess.run(
            ["git", "-C", str(repo), "hash-object", "-w", "--stdin"],
            input=text,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def test_all_present_is_a_race(self, repo):
        sha = self._blob(repo)
        assert g._only_raced_missing(repo, f"missing blob {sha}\n", "") is True

    def test_absent_object_is_not(self, repo):
        sha = self._blob(repo)
        out = f"missing blob {sha}\nmissing blob {'1' * 40}\n"
        assert g._only_raced_missing(repo, out, "") is False

    def test_any_other_line_is_not(self, repo):
        sha = self._blob(repo)
        assert g._only_raced_missing(repo, f"missing blob {sha}\n", "error: bad object") is False
        out = f"broken link from tree {sha}\n              to blob {sha}\n"
        assert g._only_raced_missing(repo, out, "") is False

    def test_no_lines_is_not(self, repo):
        assert g._only_raced_missing(repo, "", "") is False
        assert g._only_raced_missing(repo, "dangling blob abc\n", "notice: x") is False

    def test_reads_past_the_evidence_cap(self, repo):
        # 200 lines (> _EVIDENCE_CHARS): the decision must use every line, so a
        # bad line hidden past the cap still pages.
        sha = self._blob(repo)
        out = f"missing blob {sha}\n" * 200
        assert len(out) > g._EVIDENCE_CHARS
        assert g._only_raced_missing(repo, out, "") is True
        assert g._only_raced_missing(repo, out + "error: corrupt\n", "") is False

    def test_zeroed_object_is_not_present(self, repo):
        TestDeepCheck._commit_file(repo)
        sha = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD:f.txt"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        obj = repo / ".git" / "objects" / sha[:2] / sha[2:]
        size = obj.stat().st_size
        obj.chmod(0o644)
        obj.write_bytes(b"\x00" * size)
        assert g._only_raced_missing(repo, f"missing blob {sha}\n", "") is False

    def test_built_in_objects_never_vouch(self, repo):
        # git answers the empty tree from memory: batch-check says "tree 0" even
        # with its file deleted, so a missing one must page (found by this suite).
        empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        assert empty_tree in g._BUILT_IN_OBJECTS
        assert g._only_raced_missing(repo, f"missing tree {empty_tree}\n", "") is False
        for sha in g._BUILT_IN_OBJECTS:
            kind = "tree" if sha.startswith(("4b825dc", "6ef19b4")) else "blob"
            assert g._only_raced_missing(repo, f"missing {kind} {sha}\n", "") is False

    @pytest.mark.asyncio
    async def test_deleted_empty_tree_pages(self, repo, recheck_sleeps):
        empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        obj = repo / ".git" / "objects" / empty_tree[:2] / empty_tree[2:]
        assert obj.exists(), "precondition: the --allow-empty commit stores the empty tree"
        obj.unlink()
        rep = await g.check_git_deep(repo)
        assert rep.failures == ["fsck_failed"]
        assert rep.details["fsck_reproduced"] is True
        assert empty_tree in rep.details["fsck_stderr"]

    def test_replace_refs_are_ignored(self, repo):
        # A replace ref must not make an absent object look present.
        present = self._blob(repo)
        absent = "2" * 40
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", f"refs/replace/{absent}", present], check=True
        )
        assert g._only_raced_missing(repo, f"missing blob {absent}\n", "") is False

    def test_type_must_match(self, repo):
        sha = self._blob(repo)  # a blob, reported as a missing tree
        assert g._only_raced_missing(repo, f"missing tree {sha}\n", "") is False

    def test_lookup_stderr_is_not_trusted(self, repo, monkeypatch):
        # Rows that read "present" but come with stderr output (git complaining
        # while answering) are not a clean answer: page.
        sha = self._blob(repo)
        fake = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=f"{sha} blob 8\n", stderr="error: something odd\n"
        )
        monkeypatch.setattr(g.subprocess, "run", lambda *a, **k: fake)
        assert g._only_raced_missing(repo, f"missing blob {sha}\n", "") is False

    def test_sigterm_during_lookup_aborts(self, repo, monkeypatch):
        import asyncio

        sha = self._blob(repo)
        fake = subprocess.CompletedProcess(args=[], returncode=-15, stdout="", stderr="")
        monkeypatch.setattr(g.subprocess, "run", lambda *a, **k: fake)
        with pytest.raises(asyncio.CancelledError):
            g._only_raced_missing(repo, f"missing blob {sha}\n", "")

    def test_failed_lookup_is_not(self, repo, monkeypatch):
        sha = self._blob(repo)

        def _boom(*a, **k):
            raise subprocess.TimeoutExpired(cmd="git", timeout=1)

        monkeypatch.setattr(g.subprocess, "run", _boom)
        assert g._only_raced_missing(repo, f"missing blob {sha}\n", "") is False

    @pytest.mark.asyncio
    async def test_raced_recheck_reports_transient(self, repo, monkeypatch, recheck_sleeps):
        sha = self._blob(repo)
        line = f"missing blob {sha}"
        _fsck_runs(monkeypatch, (2, "missing blob aaaa", ""), (2, line, ""))
        rep = await g.check_git_deep(repo)
        assert rep.ok is True and rep.failures == []
        assert rep.details["fsck_transient"]["race"]
        assert rep.details["fsck_transient"]["lines"] == "missing blob aaaa"
        assert rep.details["fsck_transient"]["recheck_lines"] == line

    @pytest.mark.asyncio
    async def test_recheck_with_a_truly_missing_object_pages(
        self, repo, monkeypatch, recheck_sleeps
    ):
        _fsck_runs(monkeypatch, (2, _MISSING, ""), (2, _MISSING, ""))
        rep = await g.check_git_deep(repo)
        assert rep.failures == ["fsck_failed"]
        assert rep.details["fsck_reproduced"] is True


class TestProblemLines:
    def test_drops_noise_keeps_unknown_and_orders(self):
        out = "dangling tree abc\nmissing tree def\nsomething new\n\n"
        err = "warning: x\nnotice: HEAD points to an unborn branch\nerror: bad object\n"
        assert g._fsck_problem_lines(out, err).splitlines() == [
            "error: bad object",
            "missing tree def",
            "something new",
            "warning: x",
        ]

    def test_caps_with_more_marker(self):
        out = "\n".join(f"missing blob {i:040x}" for i in range(500))
        ev = g._fsck_problem_lines(out, "")
        lines = ev.splitlines()
        assert len("\n".join(lines[:-1])) <= g._EVIDENCE_CHARS
        assert lines[-1] == f"(+{500 - (len(lines) - 1)} more lines)"


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
