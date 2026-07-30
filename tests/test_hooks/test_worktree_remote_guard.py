"""Tests for the worktree-aware + remote-aware guard behavior in git_push_guard.py.

Covers three fixes:
- Change 1: merge-into-main / push-label branch is read in the dir the command
  actually targets (git -C / leading cd / payload cwd), not the hook's own cwd.
- Change 2: force push is REMOTE-aware — origin or UNKNOWN remote hard-blocks in
  every session (fail closed); a definitely-non-origin remote gets a cautious
  ask (interactive) / deny (dispatched).
- Change 3: a trailing `# merge-to-main-override` acknowledges an on-main merge.

cwd-dependent branches are driven by monkeypatching `_current_branch` /
`_resolve_push_remote` / `read_payload`, so no real git or network runs. The
deterministic remote-name cases (a literal `git push --force backups`) run the
hook via subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"
_GUARD = _SCRIPTS / "git_push_guard.py"

sys.path.insert(0, str(_SCRIPTS))
from shell_parse import analyze, git_subcommand  # noqa: E402

_FORCE = "--fo" + "rce"  # split so this file's own text never trips a host push-guard


@pytest.fixture(scope="module")
def guard_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("git_push_guard", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _payload(cmd: str, cwd: str | None = None) -> dict:
    p = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": cmd}}
    if cwd is not None:
        p["cwd"] = cwd
    return p


def _run(command: str, *, dispatched: bool = False) -> subprocess.CompletedProcess:
    """Run the guard as a subprocess (real string-parsing path, no monkeypatch)."""
    env = {
        k: v for k, v in os.environ.items() if k not in ("CLAUDE_TOOL_INPUT", "GENESIS_CC_SESSION")
    }
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    return subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps(_payload(command)),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _decision(res: subprocess.CompletedProcess) -> str | None:
    try:
        return json.loads(res.stdout)["hookSpecificOutput"]["permissionDecision"]
    except Exception:
        return None


def _run_cwd(command: str, cwd: str, *, dispatched: bool = False) -> subprocess.CompletedProcess:
    """Run the guard with a Bash-tool cwd set in the payload (real git, no mock)."""
    env = {
        k: v for k, v in os.environ.items() if k not in ("CLAUDE_TOOL_INPUT", "GENESIS_CC_SESSION")
    }
    if dispatched:
        env["GENESIS_CC_SESSION"] = "1"
    return subprocess.run(
        [sys.executable, str(_GUARD)],
        input=json.dumps(_payload(command, cwd=cwd)),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )


def _git(repo, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, text=True)


def _init_repo(path, branch: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "-c", "init.defaultBranch=main", "init", "-q")
    _git(path, "config", "user.email", "t@e.st")
    _git(path, "config", "user.name", "tester")
    (path / "base.txt").write_text("base\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    if branch != "main":
        _git(path, "checkout", "-q", "-b", branch)


@pytest.fixture
def main_repo(tmp_path):
    r = tmp_path / "main_repo"
    _init_repo(r, "main")
    return r


@pytest.fixture
def feature_repo(tmp_path):
    r = tmp_path / "feature_repo"
    _init_repo(r, "feature/x")
    return r


@pytest.fixture
def remotes_repo(tmp_path):
    """A feature-branch working repo with three remotes:

    - ``origin``  → the public repo URL
    - ``mirror``  → the SAME URL as origin (name != origin, but same repo)
    - ``backups`` → a DIFFERENT URL (a genuinely separate, private repo)
    """
    origin_url = str(tmp_path / "origin.git")
    fork_url = str(tmp_path / "fork.git")
    subprocess.run(["git", "init", "--bare", "-q", origin_url], check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", "-q", fork_url], check=True, capture_output=True)
    r = tmp_path / "wt"
    _init_repo(r, "feature/x")
    _git(r, "remote", "add", "origin", origin_url)
    _git(r, "remote", "add", "mirror", origin_url)  # same URL as origin
    _git(r, "remote", "add", "backups", fork_url)  # different URL
    return r


# ── Change 1 + 3: worktree-aware merge-into-main ─────────────────────────


class TestMergeIntoMainWorktreeAware:
    def _prep(self, guard_module, monkeypatch, cmd, cwd=None):
        monkeypatch.setattr(guard_module, "_is_dispatched", lambda: False)
        monkeypatch.setattr(guard_module, "read_payload", lambda: _payload(cmd, cwd=cwd))

    def test_merge_from_feature_worktree_not_blocked(self, guard_module, monkeypatch):
        # Effective branch (resolved in the worktree cwd) is a feature branch.
        monkeypatch.setattr(
            guard_module,
            "_current_branch",
            lambda cwd=None: "feature/x" if cwd else "main",
        )
        self._prep(guard_module, monkeypatch, "git merge upstream/feat", cwd="/wt")
        assert guard_module.main() == 0

    def test_merge_on_main_blocked(self, guard_module, monkeypatch, capsys):
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "main")
        self._prep(guard_module, monkeypatch, "git merge upstream/feat", cwd=None)
        assert guard_module.main() == 2
        assert "Merging into main" in capsys.readouterr().err

    def test_merge_on_main_with_override_allowed(self, guard_module, monkeypatch):
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "main")
        self._prep(
            guard_module,
            monkeypatch,
            "git merge upstream/feat  # merge-to-main-override",
            cwd=None,
        )
        assert guard_module.main() == 0

    def test_git_dash_C_worktree_form_not_blocked(self, guard_module, monkeypatch):
        # `git -C /wt merge feat`: effective cwd comes from the -C in argv, so the
        # branch is read in /wt (a feature branch), not the hook's main tree.
        seen = []

        def fake_branch(cwd=None):
            seen.append(cwd)
            return "feature/z" if cwd == "/wt" else "main"

        monkeypatch.setattr(guard_module, "_current_branch", fake_branch)
        self._prep(guard_module, monkeypatch, "git -C /wt merge feat", cwd=None)
        assert guard_module.main() == 0
        assert "/wt" in seen

    def test_leading_cd_worktree_form_not_blocked(self, guard_module, monkeypatch):
        monkeypatch.setattr(
            guard_module,
            "_current_branch",
            lambda cwd=None: "feature/y" if cwd == "/wt" else "main",
        )
        self._prep(guard_module, monkeypatch, "cd /wt && git merge feat", cwd=None)
        assert guard_module.main() == 0


class TestCompoundAndDecoyMerge:
    """BLOCKER 1 + BLOCKER 2: EVERY merge in a compound is checked in the dir it
    actually runs, and the LAST cd before a merge wins (not the first). Uses real
    git repos so `_current_branch` reads genuine branches."""

    def test_compound_second_bare_merge_into_main_blocked(self, main_repo, feature_repo):
        """BLOCKER 1: `git -C <feat> merge a && git merge b` — seg[0] is a feature
        branch, but the SECOND bare merge runs in the payload cwd (main) → block."""
        res = _run_cwd(
            f"git -C {feature_repo} merge a && git merge b",
            str(main_repo),
        )
        assert res.returncode == 2
        assert "Merging into main" in res.stderr

    def test_compound_both_feature_not_blocked(self, feature_repo):
        """Control: both merges resolve to feature branches → not blocked."""
        res = _run_cwd(
            f"git -C {feature_repo} merge a && git merge b",
            str(feature_repo),
        )
        assert res.returncode == 0, res.stderr

    def test_decoy_cd_last_cd_wins_blocks(self, main_repo, feature_repo):
        """BLOCKER 2: `cd <feat> && true; cd <main> && git merge x` runs in main
        (the LAST cd), so it must block — the old first-cd resolver saw <feat>."""
        res = _run_cwd(
            f"cd {feature_repo} && true; cd {main_repo} && git merge x",
            str(feature_repo),  # base cwd is feature — the trailing `cd main` overrides it
        )
        assert res.returncode == 2
        assert "Merging into main" in res.stderr

    def test_decoy_cd_last_cd_feature_not_blocked(self, main_repo, feature_repo):
        """Mirror control: last cd lands on a feature branch → not blocked."""
        res = _run_cwd(
            f"cd {main_repo} && true; cd {feature_repo} && git merge x",
            str(main_repo),
        )
        assert res.returncode == 0, res.stderr

    def test_ambiguous_cd_before_merge_fails_closed(self, guard_module, monkeypatch):
        """A cd into a variable before the merge ⇒ UNKNOWN cwd ⇒ blocked."""
        monkeypatch.setattr(guard_module, "_is_dispatched", lambda: False)
        # _current_branch would say feature, but the ambiguous cd must win → block.
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "feature/x")
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: _payload("cd $WT && git merge x", cwd="/wt"),
        )
        assert guard_module.main() == 2

    def test_merge_in_bash_c_fails_closed(self, guard_module, monkeypatch):
        """A merge nested in bash -c (depth>0) can't be cwd-associated → block."""
        monkeypatch.setattr(guard_module, "_is_dispatched", lambda: False)
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "feature/x")
        monkeypatch.setattr(
            guard_module,
            "read_payload",
            lambda: _payload("bash -c 'git merge x'", cwd="/wt"),
        )
        assert guard_module.main() == 2


# ── Change 2: remote-aware force push ────────────────────────────────────


class TestForcePushRemoteAware:
    """origin/UNKNOWN → hard-block (fail closed); non-origin → cautious ask/deny."""

    def test_force_to_origin_blocked_interactive(self):
        res = _run(f"git push {_FORCE} origin main")
        assert res.returncode == 2
        assert "Force push to origin" in res.stderr
        assert _decision(res) is None

    def test_force_to_origin_blocked_dispatched(self):
        res = _run(f"git push {_FORCE} origin main", dispatched=True)
        assert res.returncode == 2

    def test_force_to_nonorigin_asks_interactive(self, remotes_repo):
        # `backups` URL differs from origin's → genuinely different repo → ask.
        res = _run_cwd(f"git push {_FORCE} backups main", str(remotes_repo))
        assert res.returncode == 0, res.stderr
        assert _decision(res) == "ask"

    def test_force_to_nonorigin_denied_dispatched(self, remotes_repo):
        res = _run_cwd(f"git push {_FORCE} backups main", str(remotes_repo), dispatched=True)
        assert res.returncode == 2
        assert "rewrites remote history" in res.stderr
        assert _decision(res) is None

    # ── SHOULD-FIX 3: classify by URL, not remote NAME ──────────────────
    def test_force_to_mirror_same_url_as_origin_blocked(self, remotes_repo):
        """ATTACK: `git remote add mirror <origin-url>` then force-push mirror —
        a non-'origin' NAME that still rewrites the PUBLIC repo. URL == origin's
        → hard-block (was a soft ask under name-only classification)."""
        res = _run_cwd(f"git push {_FORCE} mirror main", str(remotes_repo))
        assert res.returncode == 2
        assert "Force push to origin" in res.stderr
        assert _decision(res) is None

    def test_force_to_mirror_same_url_blocked_even_dispatched(self, remotes_repo):
        res = _run_cwd(f"git push {_FORCE} mirror main", str(remotes_repo), dispatched=True)
        assert res.returncode == 2

    def test_force_to_unresolvable_url_blocked_fail_closed(self, remotes_repo):
        """A named remote with no configured URL → URL unresolvable → fail closed."""
        res = _run_cwd(f"git push {_FORCE} ghost main", str(remotes_repo))
        assert res.returncode == 2
        assert _decision(res) is None

    def test_force_unknown_remote_blocked_fail_closed(self, guard_module, monkeypatch):
        # No named remote and upstream unresolvable → remote UNKNOWN → treat as
        # origin → hard-block in every session (fail closed).
        monkeypatch.setattr(guard_module, "_is_dispatched", lambda: False)
        monkeypatch.setattr(guard_module, "_resolve_push_remote", lambda seg, cwd=None: None)
        monkeypatch.setattr(guard_module, "read_payload", lambda: _payload("git push -f"))
        assert guard_module.main() == 2

    def test_force_unknown_remote_blocked_even_dispatched(self, guard_module, monkeypatch):
        monkeypatch.setattr(guard_module, "_is_dispatched", lambda: True)
        monkeypatch.setattr(guard_module, "_resolve_push_remote", lambda seg, cwd=None: None)
        monkeypatch.setattr(guard_module, "read_payload", lambda: _payload("git push -f"))
        assert guard_module.main() == 2

    def test_nonorigin_via_upstream_asks(self, guard_module, monkeypatch, capsys):
        # No named remote, but the branch's upstream resolves to a non-origin
        # remote whose URL differs from origin's → cautious ask (not a hard block).
        monkeypatch.setattr(guard_module, "_is_dispatched", lambda: False)
        monkeypatch.setattr(guard_module, "_resolve_push_remote", lambda seg, cwd=None: "myfork")
        monkeypatch.setattr(
            guard_module,
            "_remote_url",
            lambda name, cwd=None: "url://fork" if name == "myfork" else "url://origin",
        )
        monkeypatch.setattr(guard_module, "read_payload", lambda: _payload("git push -f"))
        rc = guard_module.main()
        assert rc == 0
        out = capsys.readouterr().out
        assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "ask"
        assert "myfork" in json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]


# ── _resolve_push_remote unit tests ──────────────────────────────────────


class TestResolvePushRemote:
    def _seg(self, cmd):
        return [s for s in analyze(cmd) if git_subcommand(s.argv) == "push"][0]

    def test_named_remote(self, guard_module):
        seg = self._seg("git push origin main")
        assert guard_module._resolve_push_remote(seg) == "origin"

    def test_named_nonorigin_remote(self, guard_module):
        seg = self._seg(f"git push {_FORCE} backups main")
        assert guard_module._resolve_push_remote(seg) == "backups"

    def test_plus_refspec_is_not_a_remote(self, guard_module, monkeypatch):
        # `git push +main` names no remote → resolve via upstream.
        seg = self._seg("git push +main")
        monkeypatch.setattr(
            guard_module.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="origin/main\n", stderr=""),
        )
        assert guard_module._resolve_push_remote(seg) == "origin"

    def test_upstream_derived_remote(self, guard_module, monkeypatch):
        seg = self._seg("git push")
        monkeypatch.setattr(
            guard_module.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess([], 0, stdout="myfork/topic\n", stderr=""),
        )
        assert guard_module._resolve_push_remote(seg) == "myfork"

    def test_upstream_failure_is_unknown(self, guard_module, monkeypatch):
        seg = self._seg("git push")
        monkeypatch.setattr(
            guard_module.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess([], 1, stdout="", stderr="no upstream"),
        )
        assert guard_module._resolve_push_remote(seg) is None


# ── Change 1: push-target label uses the effective-cwd branch ────────────


def test_bare_push_label_uses_worktree_branch(guard_module, monkeypatch, capsys):
    monkeypatch.setattr(guard_module, "_is_dispatched", lambda: False)
    monkeypatch.setattr(
        guard_module,
        "_current_branch",
        lambda cwd=None: "feature/label" if cwd == "/wt" else "main",
    )
    monkeypatch.setattr(guard_module, "read_payload", lambda: _payload("git push", cwd="/wt"))
    assert guard_module.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "feature/label" in out["hookSpecificOutput"]["permissionDecisionReason"]
