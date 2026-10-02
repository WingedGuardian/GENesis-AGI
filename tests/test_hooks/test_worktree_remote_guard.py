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


@pytest.fixture
def push_url_repo(tmp_path):
    """A working repo whose ``sneaky`` remote has a PRIVATE fetch url but a PUSH
    url set to origin's url (P1-B) — git pushes to origin's repo even though the
    fetch url differs. ``backups`` stays a genuinely-separate repo."""
    origin_url = str(tmp_path / "origin.git")
    fork_url = str(tmp_path / "fork.git")
    subprocess.run(["git", "init", "--bare", "-q", origin_url], check=True, capture_output=True)
    subprocess.run(["git", "init", "--bare", "-q", fork_url], check=True, capture_output=True)
    r = tmp_path / "wt"
    _init_repo(r, "feature/x")
    _git(r, "remote", "add", "origin", origin_url)
    _git(r, "remote", "add", "sneaky", fork_url)  # private FETCH url…
    _git(r, "remote", "set-url", "--push", "sneaky", origin_url)  # …but PUSH → origin
    _git(r, "remote", "add", "backups", fork_url)  # fetch AND push → fork (disjoint)
    return r


@pytest.fixture
def sibling_trees(tmp_path):
    """Two sibling repos so a RELATIVE `cd ../main` / `git -C ../main` resolves:

    ``trees/feature`` (feature branch) and ``trees/main`` (main branch)."""
    trees = tmp_path / "trees"
    feature = trees / "feature"
    main = trees / "main"
    _init_repo(feature, "feature/x")
    _init_repo(main, "main")
    return feature, main


class TestLiveIntegrationActive:
    """The manifest alone does not make every `live` branch the integration
    branch: the target must be the repository this guard ships in."""

    @staticmethod
    def _home_with_manifest(tmp_path, monkeypatch, *, manifest: bool = True):
        home = tmp_path / "home"
        (home / ".genesis").mkdir(parents=True)
        if manifest:
            (home / ".genesis" / "deploy_manifest.json").write_text("{}\n")
        monkeypatch.setenv("HOME", str(home))

    def test_this_repository_is_active(self, guard_module, tmp_path, monkeypatch):
        self._home_with_manifest(tmp_path, monkeypatch)
        assert guard_module._live_integration_active(str(_SCRIPTS)) is True

    def test_another_repository_is_not(self, guard_module, tmp_path, monkeypatch):
        self._home_with_manifest(tmp_path, monkeypatch)
        other = tmp_path / "other"
        _init_repo(other, "live")
        assert guard_module._live_integration_active(str(other)) is False

    def test_no_manifest_is_never_active(self, guard_module, tmp_path, monkeypatch):
        self._home_with_manifest(tmp_path, monkeypatch, manifest=False)
        assert guard_module._live_integration_active(str(_SCRIPTS)) is False

    def test_unreadable_target_fails_closed(self, guard_module, tmp_path, monkeypatch):
        self._home_with_manifest(tmp_path, monkeypatch)
        assert guard_module._live_integration_active(str(tmp_path / "missing")) is True


def _common_dir_of(path) -> str:
    out = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return os.path.realpath(out)


@pytest.fixture(scope="module")
def commit_gate_module():
    from tests.conftest import private_module

    return private_module(
        "rec_manifest_binding", _SCRIPTS.parent / "review_enforcement_commit.py"
    )


class TestManifestRepoBinding:
    """Issue #2532: the deploy manifest may name the repository it belongs to in a
    top-level ``"repo"`` (that checkout's absolute git common dir). The git hooks
    (pre-commit, pre-merge-commit) and BOTH Python guards read it by one rule, and
    this table pins the two Python guards to it together:

    * no "repo" key: today's rule — armed for the repository the guard ships in,
      and for an unreadable target;
    * the key names the target repository (an existing git common dir): armed,
      even if the guard lives in a different clone, since the manifest says so;
    * the key names ANOTHER repository: not armed;
    * anything else — malformed JSON, not a JSON object, a key that is not the
      absolute path of an existing git common dir: armed for EVERY target. A
      broken manifest fails closed."""

    @staticmethod
    def _verdicts(guard_module, commit_gate_module, target):
        return (
            guard_module._live_integration_active(target),
            commit_gate_module._live_integration_repo(target),
        )

    @pytest.fixture
    def env(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / ".genesis").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        other = tmp_path / "other"
        _init_repo(other, "live")
        manifest = home / ".genesis" / "deploy_manifest.json"
        return manifest, str(_SCRIPTS), str(other)

    @pytest.mark.parametrize(
        "body, target, expected",
        [
            ("{}\n", "this", True),
            ("{}\n", "other", False),
            ("{}\n", "missing", True),
            ('{"repo": "@this"}', "this", True),
            ('{"repo": "@this"}', "other", False),
            ('{"repo": "@other"}', "other", True),
            ('{"repo": "@other"}', "this", False),
            ('{"repo": "@other"}', "missing", True),
            ("not json at all\n", "this", True),
            ("not json at all\n", "other", True),
            ('{"repo": 5}', "this", True),
            ('{"repo": 5}', "other", True),
            ('{"repo": ""}', "this", True),
            ('{"repo": "relative/.git"}', "other", True),
            ("[]\n", "this", True),
            ("[]\n", "other", True),
            # A path that is not an existing git common dir names no repository:
            # a work-tree path, or a checkout that has since moved, would
            # otherwise read as "another repository" and disarm the check.
            ('{"repo": "@otherwt"}', "this", True),
            ('{"repo": "@otherwt"}', "other", True),
            ('{"repo": "@gone"}', "this", True),
            ('{"repo": "@gone"}', "other", True),
        ],
    )
    def test_both_guards_follow_one_verdict_table(
        self, guard_module, commit_gate_module, env, tmp_path, body, target, expected
    ):
        manifest, this_repo, other_repo = env
        body = (
            body.replace("@this", _common_dir_of(this_repo))
            .replace("@otherwt", other_repo)
            .replace("@other", _common_dir_of(other_repo))
            .replace("@gone", str(tmp_path / "moved-away" / ".git"))
        )
        manifest.write_text(body)
        path = {"this": this_repo, "other": other_repo, "missing": str(tmp_path / "gone")}
        assert self._verdicts(guard_module, commit_gate_module, path[target]) == (
            expected,
            expected,
        )

    def test_the_key_is_compared_canonically(
        self, guard_module, commit_gate_module, env, tmp_path
    ):
        manifest, _this_repo, other_repo = env
        alias = tmp_path / "alias-of-git-dir"
        alias.symlink_to(_common_dir_of(other_repo))
        manifest.write_text(json.dumps({"repo": str(alias)}))
        assert self._verdicts(guard_module, commit_gate_module, other_repo) == (True, True)

    def test_no_manifest_is_never_armed(self, guard_module, commit_gate_module, env):
        manifest, this_repo, _other = env
        assert not manifest.exists()
        assert self._verdicts(guard_module, commit_gate_module, this_repo) == (False, False)

    @staticmethod
    def _shell_helper(tmp_path) -> Path:
        """The git hooks' `live_manifest_applies`, extracted verbatim from
        pre-commit (pre-merge-commit carries a byte-identical copy, pinned by the
        git-hook suite), plus a call to it."""
        text = (_SCRIPTS / "pre-commit").read_text()
        block = text[
            text.index("# >>> live-manifest-binding") : text.index("# <<< live-manifest-binding")
        ]
        helper = tmp_path / "helper.sh"
        helper.write_text(block + "\nlive_manifest_applies\n")
        return helper

    @pytest.mark.parametrize(
        "body",
        [
            "{}\n",
            "not json at all\n",
            '{"repo": 5}',
            '{"repo": "@here"}',
            '{"repo": "@elsewhere"}',
            '{"repo": "@herewt"}',
            '{"repo": "@gone"}',
        ],
    )
    def test_the_git_hooks_read_the_key_like_the_python_guards(
        self, guard_module, commit_gate_module, env, tmp_path, body
    ):
        """Four copies of one rule: two identical shell blocks and two Python
        functions. The Python pair is pinned by the table above, the shell pair
        byte for byte; this pins the shell rule to the Python one. Running in a
        repository R, the git hook is armed exactly when the Python guards do not
        find a key naming another repository."""
        manifest, this_repo, here = env
        body = (
            body.replace("@herewt", here)
            .replace("@here", _common_dir_of(here))
            .replace("@elsewhere", _common_dir_of(this_repo))
            .replace("@gone", str(tmp_path / "moved-away" / ".git"))
        )
        manifest.write_text(body)
        kind, bound = guard_module._live_manifest_binding()
        assert commit_gate_module._live_manifest_binding() == (kind, bound)
        # A git hook only ever runs in its own repository, so "no key" arms it.
        python_arms = kind != "bound" or bound == _common_dir_of(here)
        res = subprocess.run(
            ["bash", str(self._shell_helper(tmp_path))],
            cwd=here,
            env={**os.environ},
            capture_output=True,
            text=True,
        )
        assert res.returncode in (0, 1), res.stderr
        assert (res.returncode == 0) == python_arms

    @staticmethod
    def _echoing_git(tmp_path) -> str:
        """PATH with a `git` that behaves like git before 2.31 on
        `rev-parse --path-format=absolute --git-common-dir`: exit 0, the unknown
        option echoed back, then a relative `.git`."""
        shim = tmp_path / "oldgit"
        shim.mkdir()
        fake = shim / "git"
        fake.write_text("#!/bin/sh\nprintf -- '--path-format=absolute\\n.git\\n'\n")
        fake.chmod(0o755)
        return f"{shim}{os.pathsep}{os.environ['PATH']}"

    def test_an_echoed_option_is_no_repository_identity(
        self, guard_module, commit_gate_module, env, tmp_path, monkeypatch
    ):
        _manifest, _this, other = env
        monkeypatch.setenv("PATH", self._echoing_git(tmp_path))
        assert guard_module._git_common_dir(other) is None
        assert commit_gate_module._git_common_dir(other) is None

    def test_an_echoed_option_arms_the_git_hooks(self, env, tmp_path):
        # With a key naming another repository, a garbled own identity must not
        # read as "a different repository" and disarm the hook.
        manifest, this_repo, other = env
        manifest.write_text(json.dumps({"repo": _common_dir_of(this_repo)}))
        res = subprocess.run(
            ["bash", str(self._shell_helper(tmp_path))],
            cwd=other,
            env={**os.environ, "PATH": self._echoing_git(tmp_path)},
            capture_output=True,
            text=True,
        )
        assert res.returncode == 0, res.stderr


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

    def test_merge_on_live_blocked(self, guard_module, monkeypatch, capsys):
        # `live` is rebuilt by `git commit-tree`, never merged into. The message
        # names the branch that fired, not main.
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "live")
        monkeypatch.setattr(guard_module, "_live_integration_active", lambda *a: True)
        self._prep(guard_module, monkeypatch, "git merge origin/main", cwd=None)
        assert guard_module.main() == 2
        err = capsys.readouterr().err
        assert "Merging into 'live'" in err
        assert "Merging into main" not in err

    def test_merge_on_main_message_does_not_mention_live(self, guard_module, monkeypatch, capsys):
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "main")
        monkeypatch.setattr(guard_module, "_live_integration_active", lambda *a: True)
        self._prep(guard_module, monkeypatch, "git merge upstream/feat", cwd=None)
        assert guard_module.main() == 2
        assert "'live'" not in capsys.readouterr().err

    def test_main_override_does_not_waive_a_merge_on_live(self, guard_module, monkeypatch, capsys):
        # The sigil acknowledges a merge into MAIN. The git hooks refuse a merge on
        # `live` with no override, and a fast-forward reaches no git hook at all,
        # so this guard is the only thing that sees it.
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "live")
        monkeypatch.setattr(guard_module, "_live_integration_active", lambda *a: True)
        self._prep(
            guard_module,
            monkeypatch,
            "git merge --ff-only feature  # merge-to-main-override",
            cwd=None,
        )
        assert guard_module.main() == 2
        assert "Merging into 'live'" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "cmd",
        [
            "GIT_DIR=/r/.git git merge --ff-only feature  # merge-to-main-override",
            "git --git-dir=/r/.git merge feature  # merge-to-main-override",
            'cd "$X" && git merge feature  # merge-to-main-override',
            "export GIT_DIR=/r/.git; git merge feature  # merge-to-main-override",
            'bash -c "git merge feature  # merge-to-main-override"',
        ],
    )
    def test_main_override_cannot_pass_an_unresolvable_merge_where_live_exists(
        self, guard_module, monkeypatch, capsys, cmd
    ):
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "live")
        monkeypatch.setattr(guard_module, "_live_manifest_present", lambda: True)
        monkeypatch.setattr(guard_module, "_live_integration_active", lambda *a: True)
        self._prep(guard_module, monkeypatch, cmd, cwd="/wt")
        assert guard_module.main() == 2
        assert "cannot tell which branch" in capsys.readouterr().err

    def test_main_override_on_an_unresolvable_merge_without_a_manifest_is_allowed(
        self, guard_module, monkeypatch
    ):
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "main")
        monkeypatch.setattr(guard_module, "_live_manifest_present", lambda: False)
        self._prep(
            guard_module,
            monkeypatch,
            "GIT_DIR=/r/.git git merge feature  # merge-to-main-override",
            cwd="/wt",
        )
        assert guard_module.main() == 0

    def test_live_check_receives_the_directory_the_merge_runs_in(
        self, guard_module, monkeypatch
    ):
        seen = []

        def _active(cwd):
            seen.append(cwd)
            return True

        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "live")
        monkeypatch.setattr(guard_module, "_live_integration_active", _active)
        self._prep(guard_module, monkeypatch, "git -C /other merge feature", cwd="/wt")
        assert guard_module.main() == 2
        assert seen == ["/other"]

    def test_main_override_on_a_live_branch_without_a_manifest_is_allowed(
        self, guard_module, monkeypatch
    ):
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "live")
        monkeypatch.setattr(guard_module, "_live_integration_active", lambda *a: False)
        self._prep(
            guard_module, monkeypatch, "git merge feature  # merge-to-main-override", cwd=None
        )
        assert guard_module.main() == 0

    def test_merge_on_live_without_a_manifest_is_allowed(self, guard_module, monkeypatch):
        # No deploy manifest = no integration branch: `live` is just a name.
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "live")
        monkeypatch.setattr(guard_module, "_live_integration_active", lambda *a: False)
        self._prep(guard_module, monkeypatch, "git merge origin/main", cwd=None)
        assert guard_module.main() == 0

    def test_merge_on_a_branch_merely_named_like_live_is_allowed(self, guard_module, monkeypatch):
        # Exact-name match: `live-fixes` is an ordinary feature branch.
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "live-fixes")
        monkeypatch.setattr(guard_module, "_live_integration_active", lambda *a: True)
        self._prep(guard_module, monkeypatch, "git merge origin/main", cwd=None)
        assert guard_module.main() == 0

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


class TestBranchSwitchBeforeMerge:
    """Issue #2532 item 2. The merge walk reads each merge's branch ONCE, before
    the command runs. A `git checkout` / `git switch` (or a `git worktree`
    command) earlier in the SAME command changes which branch a later merge lands
    on after that read, so such a merge's branch is unknown and the walk fails
    closed on it, exactly as it does for an unresolvable directory. Decided by
    SUBCOMMAND against an allowlist of commands that cannot change the current
    branch (reads, add, commit, fetch, pull, merge), with no argv parsing:
    `git checkout live --` switches branches although it looks like a file
    restore, so no form of any other command is trusted to leave HEAD put."""

    def _prep(self, guard_module, monkeypatch, cmd, *, manifest: bool = True):
        monkeypatch.setattr(guard_module, "_is_dispatched", lambda: False)
        # Read before the command runs, the branch is an ordinary feature branch.
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "feature/x")
        monkeypatch.setattr(guard_module, "_live_manifest_present", lambda: manifest)
        monkeypatch.setattr(guard_module, "_live_integration_active", lambda *a: manifest)
        monkeypatch.setattr(guard_module, "read_payload", lambda: _payload(cmd, cwd="/wt"))

    @pytest.mark.parametrize(
        "cmd",
        [
            "git checkout live && git merge --ff-only feature",
            "git switch live; git merge feature",
            "git checkout live | git merge feature",
            "git checkout -B live origin/main && git merge feature",
            "git -C /wt switch -c live && git merge feature",
            'bash -c "git checkout live" && git merge feature',
            "(git checkout live) && git merge feature",
            "git merge $(git checkout live >/dev/null; echo feature)",
            "git worktree add /tmp/wt-live live && git -C /tmp/wt-live merge feature",
            "git worktree add /tmp/wt-live live && cd /tmp/wt-live && git merge feature",
            # Forms an argv classifier reads as a file restore or does not model:
            "git checkout live -- && git merge --ff-only feature",
            "git checkout main -- && git merge feature",
            "git rebase main live && git merge --ff-only feature",
            "git rebase origin/main main && git merge feature",
            "git checkout -- base.txt && git merge feature",  # a restore is refused too
            "git checkout HEAD~1 -- base.txt && git merge feature",
            # Not checkout/switch at all, and each lands HEAD on another branch:
            "git rebase --onto main main live && git merge --ff-only feature",
            "git stash branch tmp && git merge feature",
            "git stash -q branch tmp && git merge feature",  # the token, wherever it sits
            "git branch -M live && git merge --ff-only feature",
            "git bisect start && git merge feature",
            "gh pr checkout 5 && git merge feature",
            "git && git merge feature",  # no subcommand read: not trusted
        ],
    )
    def test_a_branch_switch_before_a_merge_is_refused(
        self, guard_module, monkeypatch, capsys, cmd
    ):
        self._prep(guard_module, monkeypatch, cmd)
        assert guard_module.main() == 2
        err = capsys.readouterr().err
        assert "before a merge" in err
        assert "SEPARATE commands" in err

    @pytest.mark.parametrize(
        "cmd, named",
        [
            ("git stash branch tmp && git merge feature", "`git stash`"),
            ("git checkout live && git merge --ff-only feature", "`git checkout`"),
            ("gh pr checkout 5 && git merge feature", "`gh`"),
            ("gh --repo o/x pr checkout 5 && git merge feature", "`gh`"),
            ('bash -c "git switch live" && git merge feature', "`git switch`"),
            ("git merge $(git rebase main live >/dev/null; echo feature)", "`git rebase`"),
        ],
    )
    def test_the_refusal_names_the_command_that_ran_first(
        self, guard_module, monkeypatch, capsys, cmd, named
    ):
        # An agent told only "a command can change the branch" cannot tell a real
        # switch from an over-cautious refusal; naming the subcommand lets it.
        # Only the executable and its subcommand are named, never the rest of argv.
        self._prep(guard_module, monkeypatch, cmd)
        assert guard_module.main() == 2
        err = capsys.readouterr().err
        assert named in err
        assert "branch tmp" not in err and "checkout 5" not in err and "o/x" not in err

    def test_the_main_override_does_not_waive_it_where_live_exists(
        self, guard_module, monkeypatch, capsys
    ):
        # The sigil acknowledges a merge into main, never into `live`, and after
        # the switch the guard cannot tell which of the two this is.
        self._prep(
            guard_module,
            monkeypatch,
            "git checkout live && git merge feature  # merge-to-main-override",
        )
        assert guard_module.main() == 2
        assert "before a merge" in capsys.readouterr().err

    def test_a_worktree_for_live_with_the_main_override_is_refused(
        self, guard_module, monkeypatch, capsys
    ):
        self._prep(
            guard_module,
            monkeypatch,
            "git worktree add /tmp/wt-live live && "
            "git -C /tmp/wt-live merge feature  # merge-to-main-override",
        )
        assert guard_module.main() == 2
        assert "before a merge" in capsys.readouterr().err

    def test_a_switch_to_main_before_a_merge_is_refused_without_a_manifest(
        self, guard_module, monkeypatch, capsys
    ):
        # The same hole reached main: the branch read before the command said
        # feature, the merge ran on main. No manifest is needed for that one.
        self._prep(
            guard_module, monkeypatch, "git checkout main && git merge feature", manifest=False
        )
        assert guard_module.main() == 2
        assert "before a merge" in capsys.readouterr().err

    def test_the_main_override_still_covers_main_without_a_manifest(
        self, guard_module, monkeypatch
    ):
        # With no `live` on this install the override means what it always meant.
        self._prep(
            guard_module,
            monkeypatch,
            "git checkout main && git merge feature  # merge-to-main-override",
            manifest=False,
        )
        assert guard_module.main() == 0

    @pytest.mark.parametrize(
        "cmd",
        [
            "git merge feature && git checkout live",  # the switch comes after
            "git status && git log -1 && git merge feature",
            "git fetch origin && git merge --ff-only origin/main",
            "git add -A && git commit -m wip && git merge feature",
            "git diff --stat && git rev-parse HEAD && git merge feature",
            # Subcommands that never change WHICH branch HEAD names (the replay of
            # real merge commands found these chained before merges):
            "git restore base.txt && git merge feature",
            "git reset --hard HEAD && git merge feature",
            "git check-attr -a base.txt && git merge feature",
            "git tag v1 && git cherry-pick abc123 && git merge feature",
            "git show-ref && git for-each-ref && git ls-tree HEAD && git merge feature",
            # Every stash form but `stash branch` leaves HEAD put; the replay found 24
            # stash segments chained before merges, none of them `stash branch`:
            "git stash && git merge origin/main && git stash pop",
            "git stash push -u -m wip && git merge feature",
            "git stash list && git merge feature",
            # `init` never moves an existing repository's HEAD (MEASURED on git 2.43:
            # a re-init ignores -b / --initial-branch / init.defaultBranch):
            "git init /tmp/scratch && git merge feature",
            "git init -b main . && git merge feature",
            "git merge feature",
            "echo git checkout live && git merge feature",  # a mention, not a switch
        ],
    )
    def test_commands_that_do_not_move_head_first_are_unaffected(
        self, guard_module, monkeypatch, cmd
    ):
        self._prep(guard_module, monkeypatch, cmd)
        assert guard_module.main() == 0

    def test_real_git_checkout_then_fast_forward_onto_live_is_refused(self, tmp_path):
        """The issue's reproduction, end to end with real git: from a feature
        branch, `git checkout live && git merge --ff-only feature` moves `live`
        and no git hook fires. The guard runs from a copy inside the scratch
        repository's own git dir, so the scratch repository IS the one it
        protects, and the manifest declares `live`."""
        import shutil

        repo = tmp_path / "repo"
        _init_repo(repo, "main")
        _git(repo, "branch", "live")
        _git(repo, "checkout", "-q", "-b", "feature")
        (repo / "f.txt").write_text("f\n")
        _git(repo, "add", "f.txt")
        _git(repo, "commit", "-qm", "feature work")
        scripts_copy = repo / ".git" / "genesis-scripts"
        shutil.copytree(
            _SCRIPTS.parent, scripts_copy, ignore=shutil.ignore_patterns("__pycache__")
        )
        guard = scripts_copy / "hooks" / "git_push_guard.py"
        home = tmp_path / "home"
        (home / ".genesis").mkdir(parents=True)
        (home / ".genesis" / "deploy_manifest.json").write_text("{}\n")
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("CLAUDE_TOOL_INPUT", "GENESIS_CC_SESSION")
        }
        env["HOME"] = str(home)
        cmd = "git checkout live && git merge --ff-only feature"
        res = subprocess.run(
            [sys.executable, str(guard)],
            input=json.dumps(_payload(cmd, cwd=str(repo))),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert res.returncode == 2, res.stdout + res.stderr
        assert "before a merge" in res.stderr
        # Guard-the-guard: run as two commands, the merge IS resolved and refused
        # as a merge into `live`, so the copy really protects this repository.
        _git(repo, "checkout", "-q", "live")
        res = subprocess.run(
            [sys.executable, str(guard)],
            input=json.dumps(_payload("git merge --ff-only feature", cwd=str(repo))),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert res.returncode == 2, res.stdout + res.stderr
        assert "Merging into 'live'" in res.stderr


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
            "_remote_push_urls",
            lambda name, cwd=None: {"url://fork"} if name == "myfork" else {"url://origin"},
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


# ── P1-A: relative cd / git -C resolved against the effective (absolute) cwd ──


class TestRelativeCwdResolution:
    def test_relative_cd_merge_into_main_blocked(self, sibling_trees):
        """`cd ../main && git merge x` from a feature-wt payload cwd resolves to
        the sibling main tree → main branch → blocked (was kept verbatim before,
        so `git -C ../main` ran from the hook cwd and mis-resolved / fell open)."""
        feature, main = sibling_trees
        res = _run_cwd("cd ../main && git merge x", str(feature))
        assert res.returncode == 2
        assert "Merging into main" in res.stderr

    def test_relative_dash_C_merge_into_main_blocked(self, sibling_trees):
        feature, main = sibling_trees
        res = _run_cwd("git -C ../main merge x", str(feature))
        assert res.returncode == 2
        assert "Merging into main" in res.stderr

    def test_relative_cd_to_feature_not_blocked(self, sibling_trees):
        """Control: relative cd landing on a feature branch → not blocked."""
        feature, main = sibling_trees
        res = _run_cwd("cd ../feature && git merge x", str(main))
        assert res.returncode == 0, res.stderr

    def test_none_branch_fails_closed(self, guard_module, monkeypatch):
        """A merge whose branch cannot be read (None) → fail closed → block."""
        monkeypatch.setattr(guard_module, "_is_dispatched", lambda: False)
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: None)
        monkeypatch.setattr(
            guard_module, "read_payload", lambda: _payload("git merge x", cwd="/wt")
        )
        assert guard_module.main() == 2

    def test_detached_head_empty_branch_allowed(self, guard_module, monkeypatch):
        """A detached HEAD ("") is not main and not None → allowed."""
        monkeypatch.setattr(guard_module, "_is_dispatched", lambda: False)
        monkeypatch.setattr(guard_module, "_current_branch", lambda cwd=None: "")
        monkeypatch.setattr(
            guard_module, "read_payload", lambda: _payload("git merge x", cwd="/wt")
        )
        assert guard_module.main() == 0


# ── P1-B: force-push classified by PUSH url set, not fetch url ────────────


class TestForcePushUrlClassification:
    def test_force_to_remote_with_origin_pushurl_blocked(self, push_url_repo):
        """`sneaky` has a private FETCH url but its PUSH url == origin's → git
        would force the PUBLIC repo → hard-block (fetch-url classification missed
        this)."""
        res = _run_cwd(f"git push {_FORCE} sneaky main", str(push_url_repo))
        assert res.returncode == 2
        assert "Force push to origin" in res.stderr
        assert _decision(res) is None

    def test_force_to_disjoint_pushurl_asks(self, push_url_repo):
        """`backups` push url is disjoint from origin's → cautious ask."""
        res = _run_cwd(f"git push {_FORCE} backups main", str(push_url_repo))
        assert res.returncode == 0, res.stderr
        assert _decision(res) == "ask"

    def test_force_to_disjoint_pushurl_denied_dispatched(self, push_url_repo):
        res = _run_cwd(f"git push {_FORCE} backups main", str(push_url_repo), dispatched=True)
        assert res.returncode == 2


# ── P1-C: --repo <dest> honored as the push destination ──────────────────


class TestForcePushRepoFlag:
    def test_force_repo_flag_origin_blocked(self, remotes_repo):
        """`git push --force --repo origin` targets origin regardless of upstream
        → hard-block (was skipped, falling back to a private upstream → soft ask)."""
        res = _run_cwd(f"git push {_FORCE} --repo origin", str(remotes_repo))
        assert res.returncode == 2
        assert "Force push to origin" in res.stderr

    def test_force_repo_equals_origin_blocked(self, remotes_repo):
        res = _run_cwd(f"git push {_FORCE} --repo=origin", str(remotes_repo))
        assert res.returncode == 2

    def test_force_repo_flag_precedence_over_positional(self, guard_module):
        seg = [
            s
            for s in analyze(f"git push {_FORCE} --repo origin backups main")
            if git_subcommand(s.argv) == "push"
        ][0]
        # --repo wins over the positional `backups`.
        assert guard_module._resolve_push_remote(seg) == "origin"
