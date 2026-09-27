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

import git_repo_selection as grs  # noqa: E402
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
    assert grs.seg_redirects_repo(_git_seg(command)) is redirected


def test_the_env_form_is_invisible_in_argv_which_is_why_the_prefix_is_read() -> None:
    """The mechanism of the hole, pinned: ``shell_parse`` strips env assignments.

    If a future refactor moves the check onto ``argv`` alone, the env form goes
    silent again — this is the test that says why the stripped prefix is read.
    """
    seg = _git_seg("GIT_DIR=/r/.git git push")
    assert not any("GIT_DIR" in tok for tok in seg.argv), "argv now carries the env assignment"
    assert grs._prefix_words(seg) == ["GIT_DIR=/r/.git"]
    assert grs.seg_redirects_repo(seg) is True


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


# --- round 2: the redirect is read from the shared parser's segments ----------
#
# Every round-1 finding was one cause: the detector matched raw WORDS anywhere in
# a command and raw FLAGS anywhere in argv, instead of asking what bash and git
# do with them. It now reads the segments ``shell_parse.analyze`` resolved, and
# asks three scoped questions (git_repo_selection's docstring).


@pytest.mark.parametrize(
    ("command", "redirected"),
    [
        # Option VALUES after the subcommand select nothing (`-o` is a push option).
        ("git push -o --git-dir=/x origin", False),
        ("git push --push-option --work-tree=/x origin", False),
        ("git merge -m --git-dir=/x feat/x", False),
        ("git push origin -- --git-dir=/x", False),
        # The GLOBAL region still counts, wherever git sits behind a wrapper.
        ("git -c a.b=c --git-dir=/x push", True),
        ("sudo git --git-dir=/x push", True),
        ("/usr/bin/git --git-dir=/x push", True),
        ("env GIT_DIR=/x git push", True),
        ("sudo GIT_DIR=/x git push", True),
        ("(GIT_DIR=/x git push)", True),
    ],
)
def test_only_the_global_region_and_the_command_prefix_count(
    command: str, redirected: bool
) -> None:
    assert grs.seg_redirects_repo(_git_seg(command)) is redirected


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m GIT_DIR && git push",
        "echo GIT_DIR && git push",
        "unset GIT_DIR && git push",
        # COMMAND-scoped: bash sets it for `git status` only.
        "GIT_DIR=/o/.git git status && git push",
        "env GIT_DIR=/o/.git git status && git push",
        "env -u GIT_DIR git status && git push",
    ],
)
def test_a_mention_or_a_command_scoped_assignment_does_not_leak_forward(command: str) -> None:
    """Round-1 finding: any word equal to a variable name was read as a
    persistent assignment, so these ordinary pushes lost their known cwd."""
    push = [s for s in analyze(command) if s.exe == "git"][-1]
    assert gpg._effective_cwd(command, {"cwd": "/here"}, seg=push) == "/here"


@pytest.mark.parametrize(
    "raw",
    [
        "export GIT_DIR=/r/.git",
        'export "GIT_DIR"=/r/.git',
        "export GIT_D\\IR=/r/.git",
        "export GIT_DIR",
        "command export GIT_DIR=/r/.git",
        "declare -x GIT_DIR=/r/.git",
        "read GIT_DIR <<< /r/.git",
        "printf -v GIT_DIR %s /r/.git",
        "GIT_DIR=/r/.git",
        "eval 'export GIT_DIR=/r/.git'",
        "export $(cat vars.env)",
    ],
)
def test_persistent_spellings_are_all_seen(raw: str) -> None:
    """Each of these leaves the variable set for the NEXT command (MEASURED in
    bash for the export / read / printf -v / quoted / escaped forms)."""
    assert grs.raw_sets_repo_env(raw) is True
    command = raw + " && git push"
    push = [s for s in analyze(command) if s.exe == "git"][-1]
    assert gpg._effective_cwd(command, {"cwd": "/here"}, seg=push) is gpg._CWD_UNKNOWN
    assert privacy._repo_redirected(command) is True


@pytest.mark.parametrize(
    "raw",
    [
        'git commit -m "fix GIT_DIR= parsing"',
        "echo 'the GIT_DIR=x spelling'",
        "git push  # GIT_DIR=",
        "MY_GIT_DIR=/r git push",
        "echo GIT_DIR",
        "unset GIT_DIR",
        "GIT_DIR=/r/.git git status",
        "export PATH=$PATH:/x",
        "source .venv/bin/activate",
    ],
)
def test_text_that_sets_nothing_for_later_commands(raw: str) -> None:
    assert grs.raw_sets_repo_env(raw) is False


def test_a_command_scoped_assignment_does_not_block_a_later_merge(repos) -> None:
    """Devin finding: `GIT_DIR=/other git status && git merge main` runs the
    merge in the ordinary checkout; it must not be refused as redirected."""
    on_main, on_feature = repos
    cmd = f"GIT_DIR={on_main}/.git git status && git merge main"
    assert _merges_onto_main(cmd, on_feature) is False


def test_a_commit_message_mentioning_the_variable_does_not_block_a_merge(repos) -> None:
    _, on_feature = repos
    assert _merges_onto_main("git commit -m GIT_DIR && git merge main", on_feature) is False
    cmd = 'git commit -m "handle GIT_DIR= spelling" && git merge main'
    assert _merges_onto_main(cmd, on_feature) is False


@pytest.mark.parametrize(
    "command",
    [
        "git branch push && GIT_DIR=/o/.git git push",
        "git push origin && GIT_DIR=/o/.git git push",
        "git push origin && git --git-dir=/o/.git push",
    ],
)
def test_the_privacy_check_reads_every_push_not_the_first_apparent_one(command: str) -> None:
    """Round-1 finding: the walk stopped at the first segment holding the words
    `git` and `push` — `git branch push` included — and never saw the redirect."""
    assert privacy._repo_redirected(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "env GIT_DIR=/r/.git git push",
        "sudo git --git-dir=/r/.git push",
        "/usr/bin/git --git-dir=/r/.git push",
    ],
)
def test_a_wrapped_redirected_push_gets_the_notice(command: str, monkeypatch) -> None:
    """Round-1 finding: these returned before the redirect check, because the
    hook's own push parser accepts only a literal `git` word — no scan and no
    notice. The redirect check now runs first, on the shared parser."""
    scanned = []
    monkeypatch.setattr(privacy, "_outgoing_diff", lambda cwd: scanned.append(cwd) or "")
    text = _advisory(command, "/here", monkeypatch)
    assert "NOT SCANNED" in text
    assert scanned == []


def test_the_privacy_scan_still_runs_after_unrelated_segments(monkeypatch) -> None:
    """CONTROL — mentions and command-scoped assignments must not suppress it."""
    scanned = []
    monkeypatch.setattr(privacy, "_targets_public_repo", lambda remote, cwd: True)
    monkeypatch.setattr(privacy, "_outgoing_diff", lambda cwd: scanned.append(cwd) or "")
    for cmd in (
        'git commit -m "handle GIT_DIR= spelling" && git -C /r push',
        "git --work-tree=/x status && git -C /r push",
        "GIT_DIR=/o/.git git status && git -C /r push",
        "git -C /r push -o --git-dir=/x",
    ):
        scanned.clear()
        text = _advisory(cmd, "/here", monkeypatch)
        assert "NOT SCANNED" not in text, cmd
        assert scanned == ["/r"], cmd


def test_both_hooks_use_the_one_shared_detector() -> None:
    """The matcher used to be duplicated in both hooks and diverged in review.
    It now has one home; this pins that neither hook grows a private copy."""
    assert gpg.seg_redirects_repo is grs.seg_redirects_repo
    assert gpg.raw_sets_repo_env is grs.raw_sets_repo_env
    for mod in (gpg, privacy):
        for name in ("_GIT_REPO_ASSIGN_RE", "_GIT_REPO_FLAGS", "_GIT_REPO_VARS"):
            assert not hasattr(mod, name), f"{mod.__name__} carries a private copy: {name}"


_TWO_PUSHES = "git push origin HEAD && GIT_DIR=/o/.git git push " + "--" + "force backup HEAD"


def test_a_second_redirected_force_push_cannot_ride_behind_an_ordinary_one(tmp_path) -> None:
    """Round-1 P1 claimed the force-push check examines only the FIRST push's cwd,
    so a later redirected force push would be judged in the wrong repository.

    It cannot reach that check: any command with two pushes is refused before
    it, so the only push the force check ever sees is the one it resolves. This
    pins that ordering, through the real hook.
    """
    payload = json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": _TWO_PUSHES},
            "cwd": str(tmp_path),
        }
    )
    res = subprocess.run(
        [sys.executable, str(_HOOKS / "git_push_guard.py")],
        input=payload,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 2, res.stderr
    assert "multiple publish/merge" in res.stderr


def test_an_append_assignment_prefix_counts_once_the_parser_resolves_the_command() -> None:
    """`GIT_DIR+=x git push` runs git with GIT_DIR set. The shared parser does not
    yet resolve that segment's command (a separate parser fix covers it); once it
    does, the stripped prefix is read here like any other assignment."""
    from types import SimpleNamespace

    seg = SimpleNamespace(exe="git", argv=["git", "push"], raw="GIT_DIR+=/r/.git git push")
    assert grs.seg_redirects_repo(seg) is True


# --- pre-push audit: spellings the segment reading must not lose --------------


@pytest.mark.parametrize(
    "command",
    [
        # A trailing backslash joins the lines in bash, so the assignment prefixes
        # the push; the segmenter splits there and reports the first half as
        # unreadable, which is what catches it. MEASURED in bash: git acts on /r.
        "GIT_DIR=/r/.git \\\n git push",
        "GIT_DIR=/r/.git\\\ngit push",
        # Parameter expansion that assigns in the current shell.
        ": ${GIT_DIR:=/r/.git} && git push",
        # A nameref: assigning the reference assigns the variable.
        "declare -n ref=GIT_DIR && export ref=/r/.git && git push",
        # Conservative: a repo-var prefix on a POSIX special builtin.
        "GIT_DIR=/r/.git : && git push",
    ],
)
def test_the_redirect_survives_continuations_and_indirect_assignment(command: str) -> None:
    push = [s for s in analyze(command) if s.exe == "git"][-1]
    assert gpg._effective_cwd(command, {"cwd": "/here"}, seg=push) is gpg._CWD_UNKNOWN
    assert privacy._repo_redirected(command) is True


def test_a_continuation_without_a_repository_variable_is_ordinary() -> None:
    """CONTROL for the continuation rule: it keys on the variable, not on `\\`."""
    command = "echo x \\\n && git push"
    push = [s for s in analyze(command) if s.exe == "git"][-1]
    assert gpg._effective_cwd(command, {"cwd": "/here"}, seg=push) == "/here"
