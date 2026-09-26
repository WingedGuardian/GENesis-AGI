"""A git command pointed at a repository by --git-dir / GIT_DIR must fail closed.

The guards model WHERE a command runs — the payload cwd, the last ``cd``,
``git -C``. git also lets a command act on a DIFFERENT repository than the one it
runs in: ``--git-dir`` / ``--work-tree``, or ``GIT_DIR`` / ``GIT_WORK_TREE`` /
``GIT_COMMON_DIR`` in the environment. None of those were modelled, in three
places:

* ``git_push_guard._effective_cwd`` — the push path. A redirected push was
  classified against the checkout it ran IN, so re-publishing a feature branch
  there made a push onto another repository's ``main`` look like a routine
  re-push, and it was allowed silently.
* ``git_push_guard._walk_merge_into_main`` — resolves the repo itself rather than
  through ``_effective_cwd``, so it carried the same hole for ``git merge`` onto
  main.
* ``pre_push_privacy_review._effective_cwd`` — knew both flags existed, but only
  as options to SKIP, and its env-assignment loop stripped ``GIT_DIR=``. So on a
  redirected push the leak scan diffed the wrong repository and reported clean.

MEASURED before this change, through the real hook, with a repository on ``main``
as the redirect target and the command run from a checkout whose feature branch
was already published:

    republish of the published branch (control)     allow
    git --git-dir=<main-repo> ... push               allow
    GIT_DIR=<main-repo> ... git push                 allow
    export GIT_DIR=... && cd <abs> && git push       allow
    git --git-dir=<main-repo> ... merge feat/x       (no decision — allowed)
    git -C <main-repo> merge feat/x (control)        BLOCKED

The fix DETECTS a redirect and reports an unknown working directory, which every
consumer already fails closed on. It does not try to resolve the redirected
repository: reimplementing git's discovery rules is how a half-model like this
comes about.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_HOOKS = _ROOT / "scripts" / "hooks"
sys.path.insert(0, str(_HOOKS))
sys.path.insert(0, str(_ROOT / "src"))

_spec = importlib.util.spec_from_file_location(
    "git_push_guard_redirect", _HOOKS / "git_push_guard.py"
)
gpg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gpg)

import pre_push_privacy_review as privacy  # noqa: E402
from shell_parse import analyze  # noqa: E402


def _git_seg(command: str):
    """The single git segment of a command, parsed the way the guard parses it."""
    segs = [s for s in analyze(command) if s.exe == "git"]
    assert len(segs) == 1, f"expected one git segment in {command!r}, got {segs}"
    return segs[0]


# --- the predicate ------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "redirected"),
    [
        ("git --git-dir=/r/.git push", True),
        ("git --git-dir /r/.git push", True),
        ("git --work-tree=/r push", True),
        ("git --work-tree /r push", True),
        ("GIT_DIR=/r/.git git push", True),
        ("GIT_WORK_TREE=/r git push", True),
        ("GIT_COMMON_DIR=/r/.git git push", True),
        # Controls — these must NOT read as a redirect, or every push asks.
        ("git -C /r push", False),
        ("git push origin HEAD", False),
        ("MY_GIT_DIR=/r git push", False),
        ("git -c user.name=x push", False),
    ],
)
def test_which_segments_redirect_the_repository(command: str, redirected: bool) -> None:
    seg = _git_seg(command)
    assert gpg._seg_redirects_repo(seg.argv, seg.raw) is redirected


def test_the_env_form_is_invisible_in_argv_which_is_why_raw_is_read() -> None:
    """The mechanism of the hole, pinned: ``shell_parse`` strips env assignments.

    If a future refactor moves the check onto ``argv`` alone, the env form goes
    silent again — this is the test that says why ``raw`` is consulted.
    """
    seg = _git_seg("GIT_DIR=/r/.git git push")
    assert not any("GIT_DIR" in tok for tok in seg.argv), "argv now carries the env assignment"
    assert gpg._seg_redirects_repo(seg.argv, None) is False
    assert gpg._seg_redirects_repo(seg.argv, seg.raw) is True


# --- _effective_cwd (push path) ---------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git --git-dir=/r/.git --work-tree=/r push",
        "GIT_DIR=/r/.git git push",
        # STICKY: an earlier export stays in force past a later absolute cd,
        # which would otherwise recover a known cwd.
        "export GIT_DIR=/r/.git && cd /elsewhere && git push",
        "GIT_WORK_TREE=/r; git push",
    ],
)
def test_a_redirected_push_has_an_unknown_working_directory(command: str) -> None:
    push = [s for s in analyze(command) if s.exe == "git"][-1]
    assert gpg._effective_cwd(command, {"cwd": "/here"}, seg=push) is gpg._CWD_UNKNOWN


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git -C /r push", "/r"),
        ("cd /r && git push", "/r"),
        ("git push", "/here"),
    ],
)
def test_the_modelled_forms_still_resolve(command: str, expected: str) -> None:
    """CONTROL — a fix that returned UNKNOWN for everything would pass the test above."""
    push = [s for s in analyze(command) if s.exe == "git"][-1]
    assert gpg._effective_cwd(command, {"cwd": "/here"}, seg=push) == expected


# --- _walk_merge_into_main, against REAL repositories -------------------------


def _repo(path: Path, branch: str) -> Path:
    path.mkdir()
    for args in (
        ["init", "-q", "-b", branch, "."],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)
    (path / "f").write_text(branch)
    subprocess.run(["git", "add", "f"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=path, check=True, capture_output=True)
    return path


@pytest.fixture
def repos(tmp_path):
    return _repo(tmp_path / "on_main", "main"), _repo(tmp_path / "on_feature", "feat/x")


def _merges_onto_main(command: str, cwd: Path) -> bool:
    merge_segs = [s for s in analyze(command) if s.exe == "git"]
    return gpg._walk_merge_into_main(command, {"cwd": str(cwd)}, merge_segs)


def test_a_redirected_merge_onto_main_is_refused(repos) -> None:
    on_main, on_feature = repos
    cmd = f"git --git-dir={on_main}/.git --work-tree={on_main} merge feat/x"
    assert _merges_onto_main(cmd, on_feature) is True


def test_an_env_redirected_merge_onto_main_is_refused(repos) -> None:
    on_main, on_feature = repos
    cmd = f"GIT_DIR={on_main}/.git GIT_WORK_TREE={on_main} git merge feat/x"
    assert _merges_onto_main(cmd, on_feature) is True


def test_an_exported_redirect_is_sticky_across_segments(repos) -> None:
    on_main, on_feature = repos
    cmd = f"export GIT_DIR={on_main}/.git && cd {on_feature} && git merge main"
    assert _merges_onto_main(cmd, on_feature) is True


def test_a_merge_on_the_feature_repository_is_still_allowed(repos) -> None:
    """CONTROL — the walk must still let an ordinary feature-branch merge through."""
    _, on_feature = repos
    assert _merges_onto_main("git merge main", on_feature) is False


# --- the privacy advisory -----------------------------------------------------


def _advisory(command: str, cwd: str, monkeypatch) -> str:
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": cwd,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    privacy.main()
    text = out.getvalue()
    return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else ""


@pytest.mark.parametrize(
    "command",
    [
        "git --git-dir=/r/.git --work-tree=/r push",
        "GIT_DIR=/r/.git git push",
        "export GIT_WORK_TREE=/r && git push",
    ],
)
def test_the_privacy_scan_says_it_did_not_run_rather_than_scanning_the_wrong_repo(
    command: str, monkeypatch
) -> None:
    """An advisory cannot block, so the honest failure is a loud one.

    Before: it diffed the checkout the command ran in and could report clean for
    commits it never looked at. The scan must NOT run at all here — a result
    from the wrong repository is worse than none.
    """
    scanned = []
    monkeypatch.setattr(privacy, "_outgoing_diff", lambda cwd: scanned.append(cwd) or "")
    text = _advisory(command, "/here", monkeypatch)
    assert "NOT SCANNED" in text
    assert scanned == [], f"the scan ran against {scanned} — the wrong repository"


def test_the_privacy_scan_still_runs_for_an_ordinary_push(monkeypatch) -> None:
    """CONTROL — the detector must not swallow ordinary pushes too."""
    scanned = []
    monkeypatch.setattr(privacy, "_targets_public_repo", lambda remote, cwd: True)
    monkeypatch.setattr(privacy, "_outgoing_diff", lambda cwd: scanned.append(cwd) or "")
    text = _advisory("git -C /r push", "/here", monkeypatch)
    assert "NOT SCANNED" not in text
    assert scanned == ["/r"]


# --- audit round: spellings, false positives, and the duplicated copies -------


@pytest.mark.parametrize(
    "raw",
    [
        'export "GIT_DIR"=/r/.git',
        "export GIT_D\\IR=/r/.git",
        "read GIT_DIR <<< /r/.git",
        "printf -v GIT_DIR %s /r/.git",
        "GIT_DIR+=/r/.git git push",
        "export GIT_DIR",
    ],
)
def test_spellings_bash_accepts_are_all_seen(raw: str) -> None:
    """Each of these was MEASURED, in real bash, to make git act on the other
    repository — and each slipped past the first version, which matched a raw
    substring. After quote removal every one is a word naming the variable."""
    assert gpg._raw_sets_repo_env(raw) is True
    assert privacy._repo_redirected(raw + " && git push") is True


@pytest.mark.parametrize(
    "raw",
    [
        'git commit -m "fix GIT_DIR= parsing"',
        "echo 'the GIT_DIR=x spelling'",
        "git push  # GIT_DIR=",
        "MY_GIT_DIR=/r git push",
    ],
)
def test_text_that_merely_mentions_the_variable_is_not_an_assignment(raw: str) -> None:
    """The false-positive direction. The substring version fired on all of these;
    measured consequence: an ordinary feature-branch merge after such a commit was
    hard-blocked, and real privacy findings were suppressed."""
    assert gpg._raw_sets_repo_env(raw) is False


def test_a_commit_message_mentioning_the_variable_does_not_block_a_merge(repos) -> None:
    _, on_feature = repos
    cmd = 'git commit -m "handle GIT_DIR= spelling" && git merge main'
    assert _merges_onto_main(cmd, on_feature) is False


def test_the_privacy_scan_still_runs_after_an_unrelated_mention(monkeypatch) -> None:
    """S1: before the fix, this ordinary push reported NOT SCANNED and dropped
    the real findings, because the whole command was searched for the text."""
    scanned = []
    monkeypatch.setattr(privacy, "_targets_public_repo", lambda remote, cwd: True)
    monkeypatch.setattr(privacy, "_outgoing_diff", lambda cwd: scanned.append(cwd) or "")
    for cmd in (
        'git commit -m "handle GIT_DIR= spelling" && git -C /r push',
        "git --work-tree=/x status && git -C /r push",
    ):
        scanned.clear()
        text = _advisory(cmd, "/here", monkeypatch)
        assert "NOT SCANNED" not in text, cmd
        assert scanned == ["/r"], cmd


def test_the_two_hooks_agree_on_what_counts_as_a_redirect() -> None:
    """N3: the privacy hook duplicates the matcher on purpose (hooks stay
    stdlib-only and import-light). Nothing else keeps the copies in step, and
    they diverged in review once already — this is the binding."""
    assert gpg._GIT_REPO_ASSIGN_RE.pattern == privacy._GIT_REPO_ASSIGN_RE.pattern
    assert gpg._GIT_REPO_FLAGS == privacy._GIT_REPO_FLAGS
    assert gpg._GIT_REPO_VARS == privacy._GIT_REPO_VARS
