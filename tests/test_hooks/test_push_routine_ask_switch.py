"""``hooks.asks.push_routine: off`` silences the push guard's ROUTINE prompts only.

Owner ruling 2026-10-06: on an install whose sessions push from linked worktrees
(``git -C <path> push``) and chain steps, the routine push prompts go silent — a
first push in any spelling, a publishing ``gh pr create``, close-then-push, a
PR-less re-push off the public repo, a re-push chained with other steps. A force
push to another remote and any other push keep asking, and so does any command
that raises one of those beside a routine prompt. Every push URL must be a
github.com https repo of the configured owner.

Same shape as ``push_publish``: silenced means NO permission decision plus a
context note, never an ``allow``. The harness is the ``push_publish`` suite's:
real git repos and remotes, network probes stubbed.
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from tests.conftest import private_module

gpg = private_module(
    "git_push_guard",
    Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "git_push_guard.py",
)

PUBLIC = "https://github.com/owner/repo"
OWNED = "https://github.com/owner/private-fork"
STRANGER = "https://github.com/stranger/repo"


def _repo(tmp_path, git_config=()) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    for args in (
        ["init", "--quiet", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["checkout", "--quiet", "-b", "feat/x"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    for kv in git_config:
        subprocess.run(
            ["git", "-C", str(repo), "config", *kv], capture_output=True, timeout=30, check=True
        )
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], capture_output=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "seed"],
        capture_output=True,
        timeout=30,
    )
    return repo


def _run(
    monkeypatch,
    tmp_path,
    capsys,
    command,
    git_config=(("remote.origin.url", PUBLIC),),
    *,
    from_elsewhere=False,
):
    """Run the guard on ``command``; ``{repo}`` in it is the test repo's path.

    ``from_elsewhere`` puts the payload cwd OUTSIDE the repo, as Claude Code's
    Bash tool does after it resets to the project root: only ``git -C`` names it.
    """
    repo = _repo(tmp_path, git_config)
    cwd = tmp_path if from_elsewhere else repo
    command = command.replace("{repo}", str(repo))
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.chdir(cwd)
    rc = gpg.main()
    out = capsys.readouterr()
    return rc, out.out, out.err


def _hso(out: str) -> dict:
    return json.loads(out)["hookSpecificOutput"]


def _decision(out: str):
    return _hso(out).get("permissionDecision") if out.strip() else None


@pytest.fixture(autouse=True)
def _first_publish(monkeypatch):
    """A first push (the branch is not on the remote), no network, foreground."""
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: False)
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)
    monkeypatch.setattr(gpg, "_remote_branch_definitely_absent", lambda *a, **k: True)
    for var in gpg._TRANSPORT_ENV:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_routine=off")


@pytest.fixture
def create_publishes(monkeypatch):
    monkeypatch.setattr(gpg, "_pr_create_would_publish", lambda argv: True)


def _assert_silenced(rc, out, err):
    assert rc == 0, (rc, out, err)
    hso = _hso(out)
    assert "permissionDecision" not in hso, out
    assert "permissionDecisionReason" not in hso, out
    assert "hooks.asks.push_routine: off" in hso["additionalContext"], out


def _assert_asks(rc, out, err):
    assert rc == 0, (rc, out, err)
    assert _decision(out) == "ask", (out, err)


# ─── silenced: the routine prompts ───────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin HEAD",
        "git status && git push -u origin HEAD",  # chained: push_publish keeps asking
    ],
)
def test_a_first_push_is_silenced(monkeypatch, tmp_path, capsys, off, command) -> None:
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, command))


def test_a_worktree_first_push_through_git_c_is_silenced(
    monkeypatch, tmp_path, capsys, off
) -> None:
    # The case that motivated the key: the Bash tool's cwd is the project root,
    # so a worktree can only be named with -C, which push_publish never accepts.
    _assert_silenced(
        *_run(
            monkeypatch, tmp_path, capsys, "git -C {repo} push -u origin HEAD", from_elsewhere=True
        )
    )


def test_a_first_push_to_another_owned_repo_is_silenced(monkeypatch, tmp_path, capsys, off) -> None:
    _assert_silenced(
        *_run(
            monkeypatch,
            tmp_path,
            capsys,
            "git push -u origin HEAD",
            (("remote.origin.url", OWNED),),
        )
    )


def test_a_publishing_pr_create_is_silenced(
    monkeypatch, tmp_path, capsys, off, create_publishes
) -> None:
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, "gh pr create --title t --body b"))


def test_close_then_repush_is_silenced(monkeypatch, tmp_path, capsys, off) -> None:
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "gh pr close 5 && git push origin HEAD")
    _assert_silenced(rc, out, err)
    assert "CLOSES a pull request" in _hso(out)["additionalContext"]


# ─── still asks ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [STRANGER, "git@github.com:owner/repo.git", "https://gitlab.com/owner/repo"],
)
def test_a_destination_outside_the_owners_github_repos_still_asks(
    monkeypatch, tmp_path, capsys, off, url
) -> None:
    rc, out, err = _run(
        monkeypatch, tmp_path, capsys, "git push -u origin HEAD", (("remote.origin.url", url),)
    )
    _assert_asks(rc, out, err)
    assert (
        "push_routine is off, but this command did not qualify"
        in (_hso(out)["permissionDecisionReason"])
    )


def test_a_raw_url_that_a_rewrite_could_move_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    _assert_asks(
        *_run(
            monkeypatch,
            tmp_path,
            capsys,
            f"git push -u {PUBLIC} HEAD",
            (
                ("remote.origin.url", PUBLIC),
                (f"url.{STRANGER}.insteadOf", PUBLIC),
            ),
        )
    )


def test_a_force_push_to_another_remote_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    rc, out, err = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push -f fork HEAD",
        (("remote.origin.url", PUBLIC), ("remote.fork.url", OWNED)),
    )
    _assert_asks(rc, out, err)
    assert "FORCE" in _hso(out)["permissionDecisionReason"]


def test_a_force_push_beside_a_routine_prompt_still_asks(
    monkeypatch, tmp_path, capsys, off, create_publishes
) -> None:
    # Non-routine wins whatever order the arms set their reasons in.
    _assert_asks(
        *_run(
            monkeypatch,
            tmp_path,
            capsys,
            "git push -f fork HEAD && gh pr create --title t",
            (("remote.origin.url", PUBLIC), ("remote.fork.url", OWNED)),
        )
    )


def test_a_push_of_the_default_branch_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push origin feat/x:main"))


@pytest.mark.parametrize("republish", [False, True])
@pytest.mark.parametrize(
    "command",
    [
        # moves HEAD after the hook read the branch (audit F1)
        "git checkout -q main && git push origin HEAD",
        "git switch main && git push -u origin HEAD",
        "git checkout -q main && git push",
        "git checkout -q --detach && git push origin HEAD",
        # moves the destination after the hook read it (audit F2)
        f"git remote set-url origin {STRANGER} && git push -u origin HEAD",
        f"git remote set-url --push origin {STRANGER} && git push origin HEAD",
        f"git config url.{STRANGER}.pushInsteadOf {PUBLIC} && git push -u origin HEAD",
    ],
)
def test_a_chained_step_is_judged_on_the_state_before_it_runs(
    monkeypatch, tmp_path, capsys, off, command, republish
) -> None:
    """PINS an owner-accepted residual (2026-10-06), not a goal: the guard reads
    the branch and destination before the command runs, so a chained step that
    moves either is classed by the state it started from and silenced. The
    hook_ask_policy docstring states this limit; if this test starts failing
    because the guard got stricter, update the docstring with it."""
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: republish)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, command))


def test_a_chained_republish_is_silenced(monkeypatch, tmp_path, capsys, off) -> None:
    # Row 4: a re-push beside a step that is not on the inert-neighbour list.
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git config core.abbrev 12 && git push origin HEAD")
    _assert_silenced(rc, out, err)
    assert "another step in this command" in _hso(out)["additionalContext"]


def test_a_prless_republish_to_an_owned_non_public_repo_is_silenced(
    monkeypatch, tmp_path, capsys, off
) -> None:
    # Row 6: off the public repo the no-open-PR arm asks rather than blocks.
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    rc, out, err = _run(
        monkeypatch, tmp_path, capsys, "git push origin HEAD", (("remote.origin.url", OWNED),)
    )
    _assert_silenced(rc, out, err)
    assert "NO OPEN PR" in _hso(out)["additionalContext"]


def test_a_prless_republish_to_a_stranger_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    _assert_asks(
        *_run(
            monkeypatch, tmp_path, capsys, "git push origin HEAD", (("remote.origin.url", STRANGER),)
        )
    )


@pytest.mark.parametrize(
    "flag", ["--repo stranger/repo", "--repo=stranger/repo", "-R stranger/repo", "--repo"]
)
def test_a_stranger_repo_flag_on_create_still_asks(
    monkeypatch, tmp_path, capsys, off, create_publishes, flag
) -> None:
    # origin is the owner's public repo; the explicit flag is what gh targets.
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, f"gh pr create --title t {flag}"))


def test_an_owned_repo_flag_on_create_is_silenced(
    monkeypatch, tmp_path, capsys, off, create_publishes
) -> None:
    _assert_silenced(
        *_run(monkeypatch, tmp_path, capsys, "gh pr create --title t --repo owner/private-fork")
    )


def test_a_policy_note_survives_a_silenced_prompt(monkeypatch, tmp_path, capsys) -> None:
    # Audit F5: a NOTE drained into the ask text must reach the silenced note too.
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_routine=off,push_publish=maybe")
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git status && git push -u origin HEAD")
    _assert_silenced(rc, out, err)
    assert "push_publish" in _hso(out)["additionalContext"]


@pytest.mark.parametrize(
    "command",
    ["gh pr create --title t", "gh pr create --title t --repo stranger/repo"],
)
def test_a_publishing_create_toward_a_stranger_still_asks(
    monkeypatch, tmp_path, capsys, off, create_publishes, command
) -> None:
    _assert_asks(
        *_run(monkeypatch, tmp_path, capsys, command, (("remote.origin.url", STRANGER),))
    )


# ─── unchanged ───────────────────────────────────────────────────────────────


def test_the_default_still_asks(monkeypatch, tmp_path, capsys) -> None:
    _assert_asks(
        *_run(
            monkeypatch, tmp_path, capsys, "git -C {repo} push -u origin HEAD", from_elsewhere=True
        )
    )


def test_a_dispatched_session_is_still_denied(monkeypatch, tmp_path, capsys, off) -> None:
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: True)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD")
    assert rc == 2 and "BLOCKED" in err, (rc, out, err)


def test_the_no_open_pr_block_on_the_public_repo_is_unchanged(
    monkeypatch, tmp_path, capsys, off
) -> None:
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push origin HEAD")
    assert rc == 2 and "NO OPEN PR" in err, (rc, out, err)
