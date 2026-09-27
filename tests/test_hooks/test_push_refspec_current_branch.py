"""Which push refspecs the push guard recognises as "update the current branch".

``_push_targets_current_branch`` decides whether a non-force push reaches the
first-push-only relaxation and the two PR-hygiene checks (no open PR,
close-then-push). A push it does not recognise falls to the push arm's
catch-all, which asks unconditionally and runs neither check.

Before this change it recognised only the bare branch name, so
``git push -u origin HEAD`` — the spelling this repo's workflow prescribes for a
branch's first publication — never reached the hygiene checks. The tests below
pin the recognised set in both directions: the spellings that DO name the
current branch (``<cur>``, ``HEAD``, ``@``, ``refs/heads/<cur>``, and
``HEAD:refs/heads/<cur>``) and the colon forms that must keep falling through
(a push onto main, a delete, a tag, another branch's tip).
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from tests.conftest import private_module

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"

gpg = private_module("git_push_guard", _HOOKS / "git_push_guard.py")


def _parsed_push_seg(command: str):
    """The push segment as main() sees it, with ``argv`` POPULATED.

    ``analyze_checked`` is the parse main() runs. A harness built on a parse that
    leaves ``argv`` empty makes the predicate refuse every input, so every
    negative row would pass for the wrong reason; the assertions below refuse that.
    """
    segs, _blind = gpg.analyze_checked(command)
    push = [s for s in segs if s.exe == "git" and gpg.git_subcommand(s.argv) == "push"]
    assert push, f"no push segment parsed out of {command!r}"
    assert getattr(push[0], "argv", None), f"argv not populated for {command!r}"
    return push[0]


@pytest.mark.parametrize(
    ("ref", "cur", "names_it", "why"),
    [
        ("feat/x", "feat/x", True, "the bare branch name"),
        ("HEAD", "feat/x", True, "resolves to the checked-out branch"),
        ("@", "feat/x", True, "git's one-character alias for HEAD"),
        ("refs/heads/feat/x", "feat/x", True, "fully qualified"),
        ("feat/y", "feat/x", False, "another branch"),
        ("main", "feat/x", False, "the default branch from a feature branch"),
        ("refs/heads/main", "feat/x", False, "same, fully qualified"),
        ("refs/tags/v1.0", "feat/x", False, "a tag is not a branch publish"),
        ("refs/heads/feat/y", "feat/x", False, "another branch, fully qualified"),
        ("@{u}", "feat/x", False, "an upstream expression is not the current branch"),
        ("HEAD", None, False, "detached: no current branch for a ref to name"),
        ("@", None, False, "same, via the alias"),
        ("HEAD", "", False, "same, empty spelling"),
        ("", "", False, "an empty ref never names anything"),
    ],
)
def test_which_refs_name_the_current_branch(ref, cur, names_it: bool, why: str) -> None:
    assert gpg._ref_names_current_branch(ref, cur) is names_it, why


@pytest.mark.parametrize(
    ("refspec", "updates_cur", "why"),
    [
        ("HEAD:refs/heads/feat/x", True, "the republish spelling"),
        ("@:refs/heads/feat/x", True, "same, through the alias"),
        ("feat/x:refs/heads/feat/x", True, "bare source naming the current branch"),
        ("refs/heads/feat/x:refs/heads/feat/x", True, "both halves fully qualified"),
        ("HEAD:feat/x", False, "unqualified dst may resolve against a tag"),
        ("HEAD:refs/heads/sub/feat/x", False, "a prefix extension, not this branch"),
        ("HEAD:refs/heads/main", False, "publishing a feature branch ONTO main"),
        (":refs/heads/feat/x", False, "empty source DELETES the remote branch"),
        ("HEAD:", False, "empty destination is not a plain update"),
        ("main:refs/heads/feat/x", False, "another branch's tip under this name"),
        ("refs/heads/main:refs/heads/feat/x", False, "same, fully qualified"),
        ("HEAD:refs/tags/v1.0", False, "a tag is not a branch publish"),
        ("HEAD:refs/heads/feat/y", False, "a differently-named branch"),
        ("HEAD:a:b", False, "two colons"),
        ("HEAD:refs/heads/feat/xy", False, "an extension of the current name is not it"),
    ],
)
def test_which_colon_refspecs_update_the_current_branch(
    refspec: str, updates_cur: bool, why: str
) -> None:
    assert gpg._colon_refspec_updates_current_branch(refspec, "feat/x") is updates_cur, why


@pytest.mark.parametrize(
    ("refspec", "cur", "why"),
    [
        (":refs/heads/feat/x", "feat/x", "an empty source DELETES the remote branch"),
        ("HEAD:a:b", "feat/x", "two colons"),
        ("HEAD:refs/heads/feat/x", None, "detached HEAD names no branch"),
        # With an EMPTY cur the expected destination is the bare prefix
        # `refs/heads/`, so this refspec passes the destination check; only the
        # `if not cur` precondition stands between it and True.
        ("HEAD:refs/heads/", "", "empty cur makes the bare prefix a match"),
    ],
)
def test_the_structural_guards_hold_even_if_the_ref_rule_goes_permissive(
    refspec: str, cur: str | None, why: str, monkeypatch
) -> None:
    """Negative control by NEUTERING the source rule rather than deleting a token.

    Today the source check alone refuses most of these inputs, so deleting the
    empty-source or detached-HEAD clause changes nothing observable. Forcing the
    source rule permissive is what shows those clauses still stand on their own
    — the case where `:refs/heads/<cur>` would otherwise become a recognised
    remote-branch DELETE.
    """
    monkeypatch.setattr(gpg, "_ref_names_current_branch", lambda ref, cur: True)
    # Control: the patch took, so a well-formed refspec now passes.
    assert gpg._colon_refspec_updates_current_branch("anything:refs/heads/feat/x", "feat/x") is True
    assert gpg._colon_refspec_updates_current_branch(refspec, cur) is False, why


def test_a_force_shorthand_refspec_never_reaches_the_colon_rule() -> None:
    """``+<refspec>`` is git's force shorthand. Both layers that keep it on the
    force arm are bound here, rather than trusting either one."""
    seg = _parsed_push_seg("git push origin +HEAD:refs/heads/feat/x")
    argv = getattr(seg, "argv", None) or []
    assert gpg._push_is_force(argv) is True
    assert gpg._push_ref_positionals(argv) is None
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=None) is False


@pytest.mark.parametrize(
    ("command", "targets_cur"),
    [
        ("git push -u origin HEAD", True),
        ("git push origin HEAD", True),
        ("git push origin @", True),
        ("git push origin feat/x", True),
        ("git push origin refs/heads/feat/x", True),
        ("git push origin HEAD:refs/heads/feat/x", True),
        ("git push origin refs/heads/main", False),
        ("git push origin refs/tags/v1.0", False),
        ("git push origin main", False),
        ("git push origin HEAD:refs/heads/main", False),
        ("git push origin :refs/heads/feat/x", False),
        ("git push origin HEAD feat/y", False),
        ("git push --all origin", False),
        ("git push --delete origin feat/x", False),
    ],
)
def test_the_rules_reach_push_targets_current_branch(
    command: str, targets_cur: bool, monkeypatch
) -> None:
    """The binding test: the helpers matter only if the predicate consults them.
    Driven through the real parse, with the repo config pinned simple so the
    rows do not depend on the host repository's own push config."""
    monkeypatch.setattr(gpg, "_push_config_is_simple", lambda *a, **k: True)
    seg = _parsed_push_seg(command)
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=None) is targets_cur


@pytest.mark.parametrize(
    ("command", "targets_cur"),
    [
        # Colon-free spellings are remapped by push config, so they need the
        # simple-config check...
        ("git push origin feat/x", False),
        ("git push origin refs/heads/feat/x", False),
        ("git push origin HEAD", False),
        ("git push origin @", False),
        # ...while a fully qualified destination is spelled out and is not.
        ("git push origin HEAD:refs/heads/feat/x", True),
    ],
)
def test_colon_free_spellings_need_simple_push_config(
    command: str, targets_cur: bool, monkeypatch
) -> None:
    monkeypatch.setattr(gpg, "_push_config_is_simple", lambda *a, **k: False)
    seg = _parsed_push_seg(command)
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=None) is targets_cur


# ─── end to end through main(): the hygiene checks now run for HEAD ──────────


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["checkout", "--quiet", "-b", "feat/x"],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
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
    command: str,
    *,
    republish: bool,
    open_prs: int,
    config: tuple[str, str] | None = None,
):
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: republish)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: open_prs)
    repo = _repo(tmp_path)
    if config is not None:
        subprocess.run(["git", "-C", str(repo), "config", *config], check=True, timeout=30)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    rc = gpg.main()
    out = capsys.readouterr()
    assert rc == 0, (rc, out.out, out.err)
    hso = json.loads(out.out)["hookSpecificOutput"]
    return hso["permissionDecision"], hso.get("permissionDecisionReason", "")


_SPELLINGS = [
    "git push -u origin HEAD",
    "git push origin @",
    "git push origin refs/heads/feat/x",
    "git push origin HEAD:refs/heads/feat/x",
]


@pytest.mark.parametrize("command", _SPELLINGS)
def test_a_first_push_still_asks(monkeypatch, tmp_path, capsys, command: str) -> None:
    """Recognising the spelling must not remove the first-push approval."""
    decision, reason = _run(monkeypatch, tmp_path, capsys, command, republish=False, open_prs=1)
    assert decision == "ask"
    assert "publishing externally" in reason


@pytest.mark.parametrize("command", _SPELLINGS)
def test_a_repush_with_no_open_pr_reports_the_gap(monkeypatch, tmp_path, capsys, command) -> None:
    """The check that never ran for these spellings. Before the fix they reached
    the catch-all's generic publish ask, which never mentions the missing PR."""
    decision, reason = _run(monkeypatch, tmp_path, capsys, command, republish=True, open_prs=0)
    assert decision == "ask"
    assert "NO OPEN PR" in reason, reason


@pytest.mark.parametrize("command", _SPELLINGS)
def test_a_repush_with_an_open_pr_rides_its_first_approval(
    monkeypatch, tmp_path, capsys, command: str
) -> None:
    """The existing re-push relaxation, now reached by the prescribed spelling."""
    decision, reason = _run(monkeypatch, tmp_path, capsys, command, republish=True, open_prs=1)
    assert decision == "allow"
    assert "re-push to 'feat/x'" in reason


def test_a_close_then_repush_is_reported(monkeypatch, tmp_path, capsys) -> None:
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "gh pr close 5 && git push -u origin HEAD",
        republish=True,
        open_prs=1,
    )
    assert decision == "ask"
    assert "CLOSES a pull request" in reason, reason


@pytest.mark.parametrize(
    "config",
    [
        ("remote.origin.push", "refs/heads/feat/x:refs/heads/main"),
        ("push.default", "upstream"),
    ],
)
@pytest.mark.parametrize(
    "command",
    [
        "git push origin feat/x",
        "git push origin refs/heads/feat/x",
        "git push -u origin HEAD",
        "git push origin @",
    ],
)
def test_push_config_that_remaps_the_ref_keeps_the_ask(
    monkeypatch, tmp_path, capsys, command: str, config
) -> None:
    """MEASURED with git 2.43: under either config, `git push origin <cur>` and
    `git push origin refs/heads/<cur>` updated `main`, not `<cur>`. A re-push that
    git would send elsewhere must not ride the first push's approval, so the
    colon-free spellings keep the ask even when republished with an open PR."""
    decision, reason = _run(
        monkeypatch, tmp_path, capsys, command, republish=True, open_prs=1, config=config
    )
    assert decision == "ask", reason
    assert "publishing externally" in reason


def test_a_fully_qualified_destination_is_not_remapped(monkeypatch, tmp_path, capsys) -> None:
    """Control: with the remap configured, `HEAD:refs/heads/<cur>` still rides
    the first approval — its destination is spelled out (MEASURED to update
    `<cur>` under both remapping configs)."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push origin HEAD:refs/heads/feat/x",
        republish=True,
        open_prs=1,
        config=("remote.origin.push", "refs/heads/feat/x:refs/heads/main"),
    )
    assert decision == "allow", reason


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD:refs/heads/main",
        "git push origin refs/heads/main",
        "git push origin refs/tags/v1.0",
    ],
)
def test_unrecognised_refs_keep_the_catch_all_ask(monkeypatch, tmp_path, capsys, command) -> None:
    """Even when everything looks republished and PR-covered, a push that does
    not update the current branch is never silently allowed."""
    decision, reason = _run(monkeypatch, tmp_path, capsys, command, republish=True, open_prs=1)
    assert decision == "ask"
    assert "publishing externally" in reason
