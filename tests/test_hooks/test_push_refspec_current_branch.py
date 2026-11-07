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
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.conftest import private_module

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"

gpg = private_module("git_push_guard", _HOOKS / "git_push_guard.py")


@pytest.fixture(autouse=True)
def _isolate_git_config(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    # Stock ssh transport, as these cases model: the guard declines to probe when
    # either is set, and the suite's live closure sets GIT_SSH_COMMAND=false.
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)


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


def _dry_run_verdict(kind: str = "republish", flag: str | None = None, src="HEAD", dst=None):
    if flag is None:
        flag = "=" if kind == "republish" else "*"
    if dst is None:
        dst = "refs/heads/feat/x"
    block = gpg._DryRunBlock(
        "file:///r.git",
        (gpg._DryRunRef(flag, src, dst),),
    )
    return SimpleNamespace(kind=kind, block=block)


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


def test_a_non_push_argv_is_not_a_bare_push(monkeypatch) -> None:
    """The ``None`` sentinel: a non-push argv must refuse, not read as bare push.

    ``push_arg_stream`` returns ``None`` for ``git status`` and an EMPTY stream
    for bare ``git push``; that distinction keeps the empty positional list from
    passing the allowlist for a segment that never pushes.
    """
    monkeypatch.setattr(gpg, "_push_config_is_simple", lambda *a, **k: True)
    # Bare `git push` is the legitimate empty stream — this row must NOT change.
    assert gpg._push_ref_positionals(["git", "push"]) == []
    for argv in (["git", "status"], ["git", "-P", "merge"], ["git"]):
        assert gpg._push_ref_positionals(argv) is None, argv
    segs, _blind = gpg.analyze_checked("git status")
    seg = next(s for s in segs if s.exe == "git")
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=None) is False


def test_shared_push_arg_stream_uses_typed_canonical_tokens() -> None:
    argv = ["git", "-c", "+k=v", "push", "-uo", "-f", "--mirr", "origin", "+main"]
    assert gpg.push_arg_stream(["git", "status"]) is None
    assert gpg.push_arg_stream(["git", "push"]) == []
    assert gpg.push_arg_stream(argv) == [
        gpg.PushArg(gpg.PushArgKind.GLOBAL_VALUE, "-c"),
        gpg.PushArg(gpg.PushArgKind.SHORT, "u"),
        gpg.PushArg(gpg.PushArgKind.SHORT, "o"),
        gpg.PushArg(gpg.PushArgKind.SHORT_VALUE, "-f"),
        gpg.PushArg(gpg.PushArgKind.LONG, "--mirror"),
        gpg.PushArg(gpg.PushArgKind.POSITIONAL, "origin"),
        gpg.PushArg(gpg.PushArgKind.PLUS, "+main"),
    ]


@pytest.mark.parametrize(
    ("option", "canonical"),
    [
        ("--b", "--branches"),
        ("--br", "--branches"),
        ("--veri", "--verify"),
        ("--ver", "--ver"),
    ],
)
def test_abbreviated_push_options_match_git_canonicalization(option: str, canonical: str) -> None:
    assert gpg.push_arg_stream(["git", "push", option, "origin"]) == [
        gpg.PushArg(gpg.PushArgKind.LONG, canonical),
        gpg.PushArg(gpg.PushArgKind.POSITIONAL, "origin"),
    ]


def test_force_capable_up_to_date_push_refspec_keeps_the_ask(monkeypatch, tmp_path, capsys) -> None:
    measure_dir = tmp_path / "measure"
    measure_dir.mkdir()
    repo, _remotes = _lab_repo(measure_dir, monkeypatch)
    _git(repo, "config", "remote.origin.push", "+refs/heads/feat/x:refs/heads/feat/x")
    blocks = gpg._push_dry_run(["origin"], str(repo))
    assert blocks is not None and len(blocks) == 1
    assert len(blocks[0].refs) == 1
    assert blocks[0].refs[0].flag == "="
    assert blocks[0].refs[0].dst == "refs/heads/feat/x"

    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push origin",
        republish=True,
        open_prs=1,
        config=("remote.origin.push", "+refs/heads/feat/x:refs/heads/feat/x"),
        dry_run_verdict=None,
    )
    assert decision == "ask", reason


def test_push_config_that_remaps_main_to_current_branch_keeps_the_ask(
    monkeypatch, tmp_path, capsys
) -> None:
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push origin",
        republish=True,
        open_prs=1,
        config=("remote.origin.push", "refs/heads/main:refs/heads/feat/x"),
        git_setup=[["push", "--force", "--no-verify", "origin", "main:refs/heads/feat/x"]],
        dry_run_verdict=None,
    )
    assert decision == "ask", reason


def test_mirror_config_is_rejected_before_the_dry_run(tmp_path, monkeypatch) -> None:
    repo, _remotes = _lab_repo(tmp_path, monkeypatch)
    _git(repo, "config", "remote.origin.mirror", "true")
    pushed = []
    run = gpg.subprocess.run

    def spy(argv, *args, **kwargs):
        if "push" in argv:
            pushed.append(argv)
        return run(argv, *args, **kwargs)

    monkeypatch.setattr(gpg.subprocess, "run", spy)
    seg = _parsed_push_seg("git push origin")
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=str(repo)) is False
    assert pushed == []


def test_named_remote_config_is_checked_even_if_a_matching_path_exists(
    tmp_path, monkeypatch
) -> None:
    repo, remotes = _lab_repo(tmp_path, monkeypatch)
    (repo / "origin").mkdir()
    _git(repo, "config", "remote.origin.push", "+refs/heads/feat/x:refs/heads/feat/x")
    assert _git(repo, "remote", "get-url", "--push", "--all", "origin").stdout.strip() == str(
        (remotes / "r.git").resolve().as_uri()
    )
    seg = _parsed_push_seg("git push origin")
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=str(repo)) is False


def test_ext_pushurl_is_rejected_without_running_its_helper(monkeypatch, tmp_path, capsys) -> None:
    marker = tmp_path / "ext-ran"
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push origin HEAD",
        republish=True,
        open_prs=1,
        config=[
            ("remote.origin.pushurl", f"ext::sh -c 'touch {marker}'"),
            ("protocol.ext.allow", "always"),
        ],
        dry_run_verdict=None,
    )
    assert decision == "ask", reason
    assert not marker.exists()


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
        ("git push --branches origin", False),
        ("git push --b origin", False),
        ("git push --delete origin feat/x", False),
    ],
)
def test_the_rules_reach_push_targets_current_branch(
    command: str, targets_cur: bool, monkeypatch
) -> None:
    """The local shape and main's full config predicate decide qualification."""
    monkeypatch.setattr(gpg, "_push_config_is_simple", lambda *a, **k: True)
    seg = _parsed_push_seg(command)
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=None) is targets_cur


_RECOGNISED = [
    "git push",
    "git push origin",
    "git push origin feat/x",
    "git push origin refs/heads/feat/x",
    "git push -u origin HEAD",
    "git push origin @",
    "git push origin HEAD:refs/heads/feat/x",
    "git push origin @:refs/heads/feat/x",
]


@pytest.mark.parametrize("command", _RECOGNISED)
def test_every_recognised_spelling_passes_the_local_gate(command: str, monkeypatch) -> None:
    """The target gate checks the push shape and full config; verdicts belong to the caller."""
    seg = _parsed_push_seg(command)
    monkeypatch.setattr(gpg, "_push_config_is_simple", lambda *a, **k: True)
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=None) is True
    monkeypatch.setattr(gpg, "_push_config_is_simple", lambda *a, **k: False)
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=None) is False


@pytest.mark.parametrize(
    "command",
    [
        # config the push supplies ITSELF — invisible to the repo-config reads
        "git -c remote.origin.receivepack=/x/helper push origin HEAD",
        "git -c push.recurseSubmodules=on-demand push origin HEAD:refs/heads/feat/x",
        "git --config-env=push.followTags=HOME push origin feat/x",
        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=push.followTags GIT_CONFIG_VALUE_0=true "
        "git push origin feat/x",
        "env GIT_CONFIG_COUNT=1 git push origin HEAD",
        "HOME=/elsewhere git push origin HEAD",
        # other global options that change what or where
        "git --exec-path=/x push origin HEAD",
        "git --namespace=x push origin HEAD",
        "git --git-dir=/x/.git push origin HEAD",
    ],
)
def test_a_push_carrying_its_own_config_is_not_plain(command: str, monkeypatch) -> None:
    """Push-supplied config is rejected by the local argument-shape checks."""
    monkeypatch.setattr(gpg, "_push_config_is_simple", lambda *a, **k: True)
    seg = _parsed_push_seg(command)
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=None) is False


@pytest.mark.parametrize(
    "command",
    ["git -C /some/dir push origin HEAD", "git -P push origin HEAD", "git --no-pager push"],
)
def test_the_allowlisted_global_options_still_pass(command: str, monkeypatch) -> None:
    monkeypatch.setattr(gpg, "_push_config_is_simple", lambda *a, **k: True)
    seg = _parsed_push_seg(command)
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=None) is True


@pytest.mark.parametrize(
    ("command", "dry"),
    [
        ("git push --dry-run origin HEAD", True),
        ("git push --dry origin HEAD", True),
        ("git push -n origin HEAD", True),
        ("git push -un origin HEAD", True),
        # a push-option VALUE that spells a dry-run flag is data (git-push(1):
        # -o takes the next argument) — the push is real
        ("git push -o --dry-run origin HEAD", False),
        ("git push -o -n origin HEAD", False),
        ("git push --push-option --dry-run origin HEAD", False),
        ("git push -uo --dry-run origin HEAD", False),
        ("git push -on origin HEAD", False),
        ("git push -o ci.skip -n origin HEAD", True),
        ("git -C /x push origin HEAD", False),
        # the LAST spelling wins (MEASURED, git 2.43: these really push)
        ("git push --dry --no-dry-run origin HEAD", False),
        ("git push -n --no-dry-run origin HEAD", False),
        ("git push --no-dry-run --dry origin HEAD", True),
        ("git push --no-dry-run -n origin HEAD", True),
    ],
)
def test_dry_run_detection_skips_option_values(command: str, dry: bool) -> None:
    assert gpg._push_is_dry_run(_parsed_push_seg(command)) is dry


# ─── one scanner for every consumer: a bundled -o value is never a positional ──


@pytest.mark.parametrize(
    ("command", "remote", "positionals", "why"),
    [
        ("git push -uo ci.skip origin HEAD", "origin", ["origin", "HEAD"], "the defect"),
        ("git push -u origin HEAD", "origin", ["origin", "HEAD"], "control: no bundle"),
        ("git push -oci.skip origin HEAD", "origin", ["origin", "HEAD"], "glued value"),
        ("git push -o ci.skip origin HEAD", "origin", ["origin", "HEAD"], "separate value"),
        ("git push --push-option ci.skip origin HEAD", "origin", ["origin", "HEAD"], "long form"),
        ("git push --push-option=ci.skip origin HEAD", "origin", ["origin", "HEAD"], "glued long"),
        ("git push --push x origin HEAD", "origin", ["origin", "HEAD"], "unique long abbreviation"),
        (
            "git push --recurse-submodules check rb HEAD:x",
            "rb",
            ["rb", "HEAD:x"],
            "separate-value option",
        ),
        ("git push --recurse check rb HEAD:x", "rb", ["rb", "HEAD:x"], "unique long abbreviation"),
        ("git push -fo x origin HEAD", "origin", ["origin", "HEAD"], "-f + -o value in one bundle"),
        (
            "git push -o --repo=fork origin +main",
            "origin",
            ["origin"],
            "repo spelling is an -o value",
        ),
        (
            "git push origin +HEAD:refs/heads/feat/x",
            "origin",
            ["origin"],
            "+refspec is not a remote",
        ),
    ],
)
def test_an_option_value_is_never_a_remote_or_refspec(
    command: str, remote: str, positionals: list[str], why: str
) -> None:
    argv = getattr(_parsed_push_seg(command), "argv", None) or []
    assert gpg._push_named_remote(argv) == remote, why
    assert gpg._push_positionals(argv) == positionals, why


@pytest.mark.parametrize(
    ("command", "force", "why"),
    [
        ("git push -o +ci.skip origin HEAD", False, "the -o value is data, not a refspec"),
        ("git push -o -f origin HEAD", False, "same — a value that spells -f"),
        ("git push -o --force origin HEAD", False, "same — the long flag spelled as a value"),
        ("git push -uo -f origin HEAD", False, "a bundle-ending -o takes the NEXT token as value"),
        ("git push -uo +x origin HEAD", False, "same — a + leading value is not a force refspec"),
        ("git push -oci.skip origin HEAD", False, "glued value, no force letter"),
        ("git push -of origin HEAD", False, "-of glues 'f' AS the -o value"),
        ("git push --push-option=-f origin HEAD", False, "glued long value"),
        ("git -c +k=v push origin HEAD", False, "a -c value is data even before push"),
        ("git push -f origin HEAD", True, "control: a real force flag"),
        ("git push -fo x origin HEAD", True, "control: -f inside the bundle"),
        ("git push origin +HEAD", True, "control: +refspec"),
        ("git push --force-with-lease origin HEAD", True, "control: long force form"),
        ("git push --mirror origin", True, "control: mirror is unconditional force"),
        ("git push --mirr origin", True, "unique abbreviation of mirror"),
        ("git push --mir origin", True, "unique abbreviation of mirror"),
        ("git push --force-w origin main", True, "unique abbreviation of force-with-lease"),
        ("git push --forc origin main", False, "ambiguous force prefix is not a force flag"),
        # the LAST spelling wins (MEASURED, git 2.43: each rejected as non-fast-forward)
        ("git push --force --no-force origin x", False, "a later --no-force clears it"),
        ("git push -f --no-force origin x", False, "-f and --force share one variable"),
        ("git push --force-w --no-force-with-lease origin x", False, "lease cleared"),
        ("git push --mirr --no-mirror origin", False, "mirror cleared"),
        ("git push --force --no-mirror origin x", False, "mirror clears the shared force bit"),
        ("git push -f --no-mir origin x", False, "abbreviated no-mirror clears the shared bit"),
        ("git push --no-force --force origin x", True, "re-enabled after the negation"),
        ("git push --no-mirror --force origin x", True, "force re-enables after no-mirror"),
        ("git push --mirror --no-mirror --force origin x", True, "last force spelling wins"),
        ("git push --force-if-includes origin x", False, "includes alone is not force"),
        ("git push --force-with-lease --force-if-includes origin x", True, "includes with a lease"),
        ("git push --force-if-includes --force origin x", True, "explicit force still applies"),
        ("git push --force-with-lease --no-force-with-lease --force origin x", True, "force"),
        ("git push --no-force origin +x", True, "+refspec is not undone by a flag"),
    ],
)
def test_an_option_value_is_never_a_force(command: str, force: bool, why: str) -> None:
    argv = getattr(_parsed_push_seg(command), "argv", None) or []
    assert gpg._push_is_force(argv) is force, why


@pytest.mark.parametrize(
    ("command", "positionals", "force"),
    [
        ("git push -- origin HEAD", ["origin", "HEAD"], False),
        ("git push origin -- -f", ["origin", "-f"], False),
    ],
)
def test_double_dash_ends_push_option_scanning(
    command: str, positionals: list[str], force: bool
) -> None:
    argv = getattr(_parsed_push_seg(command), "argv", None) or []
    assert gpg._push_positionals(argv) == positionals
    assert gpg._push_is_force(argv) is force


@pytest.mark.parametrize(
    ("command", "positionals"),
    [
        ("git push --set-up origin HEAD", ["origin", "HEAD"]),
        ("git push --no-ver origin HEAD", None),
    ],
)
def test_abbreviated_push_flags_use_the_canonical_safe_flag(
    command: str, positionals: list[str] | None
) -> None:
    argv = getattr(_parsed_push_seg(command), "argv", None) or []
    assert gpg._push_ref_positionals(argv) == positionals


@pytest.mark.parametrize(
    ("command", "repo", "why"),
    [
        ("git push --repo dest origin", "dest", "control: the real flag"),
        ("git push --repo=dest", "dest", "control: the glued form"),
        ("git push -o --repo origin HEAD", None, "-o's value is data, not the flag"),
        ("git push --push-option=--repo origin", None, "same, glued long form"),
        ("git push origin HEAD", None, "no --repo at all"),
        ("git push --rep backups --repo origin --all", "origin", "git keeps the last --repo"),
        ("git push --repo origin --rep=backups", "backups", "last wins, abbreviated"),
        ("git push --repo backups --no-repo --all", None, "a later --no-repo clears it"),
        ("git push --no-repo --repo dest", "dest", "--repo after --no-repo counts"),
    ],
)
def test_a_repo_spelling_inside_an_option_value_is_not_a_repo_flag(
    command: str, repo: str | None, why: str
) -> None:
    argv = getattr(_parsed_push_seg(command), "argv", None) or []
    assert gpg._push_repo_flag(argv) == repo, why


@pytest.mark.parametrize(
    ("command", "remote"),
    [
        ("git push --repo backups origin main", "origin"),
        ("git push --repo origin backups main", "backups"),
        ("git push --repo=backups", "backups"),
        ("git push --rep=backups", "backups"),
    ],
)
def test_resolve_push_remote_prefers_positional_repository(command: str, remote: str) -> None:
    assert gpg._resolve_push_remote(_parsed_push_seg(command)) == remote


def test_push_option_repo_value_does_not_override_positional_destination() -> None:
    seg = _parsed_push_seg("git push -o --repo=fork origin +main")
    argv = getattr(seg, "argv", None) or []
    assert gpg._resolve_push_remote(seg) == "origin"
    assert gpg._push_is_force(argv) is True


# ─── end to end through main(): the hygiene checks now run for HEAD ──────────


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["remote", "add", "origin", (tmp_path / "remotes" / "r.git").resolve().as_uri()],
    ):
        subprocess.run(["git", "-C", str(repo), *args], capture_output=True, timeout=30)
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "base.txt"], capture_output=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "base"],
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "--quiet", "-b", "feat/x"],
        capture_output=True,
        timeout=30,
    )
    (repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], capture_output=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "seed"],
        capture_output=True,
        timeout=30,
    )
    return repo


def _local_remotes(tmp_path: Path, monkeypatch) -> Path:
    remotes = tmp_path / "remotes"
    remotes.mkdir()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name in ("r.git", "other.git", "second.git"):
        subprocess.run(
            ["git", "init", "--bare", "--quiet", str(remotes / name)],
            check=True,
            timeout=30,
        )
    return remotes


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, timeout=30
    )


def _lab_repo(tmp_path: Path, monkeypatch, *, republish: bool = True) -> tuple[Path, Path]:
    remotes = _local_remotes(tmp_path, monkeypatch)
    repo = _repo(tmp_path)
    if republish:
        _git(
            repo,
            "push",
            "--no-verify",
            "--recurse-submodules=no",
            "origin",
            "HEAD:refs/heads/feat/x",
        )
    return repo, remotes


def _run(
    monkeypatch,
    tmp_path,
    capsys,
    command: str,
    *,
    republish: bool,
    open_prs: int,
    config: tuple[str, str] | list[tuple[str, str]] | None = None,
    git_setup: list[list[str]] | None = None,
    legacy_remote_file: str | None = None,
    dry_run_verdict: bool | None = None,
    config_text: str | None = None,
    hook_env: dict[str, str] | None = None,
    probe_argv: list[list[str]] | None = None,
):
    remotes = _local_remotes(tmp_path, monkeypatch)
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)
    if dry_run_verdict is not None:
        verdict = _dry_run_verdict("republish" if republish else "first")
        monkeypatch.setattr(
            gpg,
            "_push_dry_run_verdict",
            lambda *a, **k: verdict if dry_run_verdict else None,
        )
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: open_prs)
    repo = _repo(tmp_path)
    if republish:
        _git(
            repo,
            "push",
            "--no-verify",
            "--recurse-submodules=no",
            "origin",
            "HEAD:refs/heads/feat/x",
        )
    configs = [config] if isinstance(config, tuple) else (config or [])
    for pair in configs:
        resolved = [
            part.replace("{r}", (remotes / "r.git").resolve().as_uri()).replace(
                "{other}", (remotes / "other.git").resolve().as_uri()
            )
            for part in pair
        ]
        subprocess.run(["git", "-C", str(repo), "config", *resolved], check=True, timeout=30)
    for args in git_setup or []:
        subprocess.run(["git", "-C", str(repo), *args], check=True, timeout=30)
    if config_text is not None:
        config_text = config_text.replace("{r}", (remotes / "r.git").resolve().as_uri())
        config_path = repo / ".git" / "config"
        config_path.write_text(config_path.read_text() + "\n" + config_text)
    if legacy_remote_file:
        subprocess.run(
            ["git", "-C", str(repo), "remote", "remove", "origin"],
            check=True,
            timeout=30,
        )
        path = repo / ".git" / legacy_remote_file / "origin"
        path.parent.mkdir(parents=True, exist_ok=True)
        url = (remotes / "r.git").resolve().as_uri()
        if legacy_remote_file == "remotes":
            path.write_text(f"URL: {url}\nPush: HEAD:refs/heads/main\n")
        else:
            path.write_text(f"{url}#main\n")
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    for key, value in (hook_env or {}).items():
        monkeypatch.setenv(key, value)
    if probe_argv is not None:
        real_run = gpg.subprocess.run

        def record_probe(argv, *args, **kwargs):
            if isinstance(argv, (list, tuple)) and (
                "ls-remote" in argv or ("push" in argv and "--dry-run" in argv)
            ):
                probe_argv.append(list(argv))
            return real_run(argv, *args, **kwargs)

        monkeypatch.setattr(gpg.subprocess, "run", record_probe)
    rc = gpg.main()
    out = capsys.readouterr()
    assert rc == 0, (rc, out.out, out.err)
    hso = json.loads(out.out)["hookSpecificOutput"]
    return hso["permissionDecision"], hso.get("permissionDecisionReason", "")


def _write_program_stub(path: Path, marker: Path) -> None:
    path.write_text(f"#!/bin/sh\nprintf x > {shlex.quote(str(marker))}\nexit 1\n")
    path.chmod(0o755)


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


@pytest.mark.parametrize(
    ("command", "canonical"),
    [
        ("git push --branches origin", "--branches"),
        ("git push --b origin", "--branches"),
    ],
)
def test_broadening_options_keep_a_repush_on_ask(
    monkeypatch, tmp_path, capsys, command: str, canonical: str
) -> None:
    stream = gpg.push_arg_stream(_parsed_push_seg(command).argv)
    assert stream and stream[0] == gpg.PushArg(gpg.PushArgKind.LONG, canonical)
    decision, _reason = _run(monkeypatch, tmp_path, capsys, command, republish=True, open_prs=1)
    assert decision == "ask"


def test_a_bundled_push_option_repushes_like_the_plain_spelling(
    monkeypatch, tmp_path, capsys
) -> None:
    """The user-visible defect: ``-uo`` hid ``origin`` behind -o's value, so the
    remote resolved to ``ci.skip`` and an eligible re-push asked again instead
    of riding its first approval."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push -uo ci.skip origin HEAD",
        republish=True,
        open_prs=1,
    )
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


def test_configured_push_refspec_remapping_keeps_the_ask(monkeypatch, tmp_path, capsys) -> None:
    """Cover an explicit remote with no refspec, outside the matrix below."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push origin",
        republish=True,
        open_prs=1,
        config=("remote.origin.push", "HEAD:refs/heads/main"),
    )
    assert decision == "ask", reason
    assert "publishing externally" in reason


def test_push_default_upstream_remapping_keeps_the_ask(monkeypatch, tmp_path, capsys) -> None:
    """Cover a bare push, outside the explicit-refspec matrix below."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push",
        republish=True,
        open_prs=1,
        config=("push.default", "upstream"),
        git_setup=[
            ["push", "--no-verify", "origin", "feat/x:refs/heads/main"],
            ["fetch", "origin", "main"],
            ["branch", "--set-upstream-to=origin/main", "feat/x"],
        ],
    )
    assert decision == "ask", reason
    assert "publishing externally" in reason


@pytest.mark.parametrize(
    "command",
    [
        "git push origin feat/x",
        "git push origin refs/heads/feat/x",
        "git push -u origin HEAD",
        "git push origin @",
        "git push origin HEAD:refs/heads/feat/x",
    ],
    ids=["short-branch", "full-branch", "set-upstream", "at", "qualified"],
)
def test_configured_push_refspec_keeps_the_local_ask_before_dry_run(
    monkeypatch, tmp_path, capsys, command: str
) -> None:
    """Any configured remote refspec is rejected locally, even when a direct dry run would target this branch."""
    monkeypatch.setattr(
        gpg,
        "_push_dry_run_verdict",
        lambda *a, **k: pytest.fail("configured push refspec reached the dry run"),
    )
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        command,
        republish=True,
        open_prs=1,
        config=("remote.origin.push", "refs/heads/feat/x:refs/heads/main"),
    )
    assert decision == "ask", reason


@pytest.mark.parametrize(
    "command",
    [
        "git push origin feat/x",
        "git push origin refs/heads/feat/x",
        "git push -u origin HEAD",
        "git push origin @",
        "git push origin HEAD:refs/heads/feat/x",
    ],
    ids=["short-branch", "full-branch", "set-upstream", "at", "qualified"],
)
def test_push_default_upstream_keeps_main_ask_without_dry_run(
    monkeypatch, tmp_path, capsys, command: str
) -> None:
    """Main refuses push.default=upstream before the dry-run classifier."""
    monkeypatch.setattr(
        gpg, "_push_dry_run", lambda *a, **k: pytest.fail("upstream push reached the dry run")
    )
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        command,
        republish=True,
        open_prs=1,
        config=("push.default", "upstream"),
        git_setup=[
            ["push", "--no-verify", "origin", "feat/x:refs/heads/main"],
            ["fetch", "origin", "main"],
            ["branch", "--set-upstream-to=origin/main", "feat/x"],
        ],
    )
    assert decision == "ask", reason
    assert "publishing externally" in reason


_SIDE_CHANNELS = [
    # (config, what git does with it — MEASURED with git 2.43 unless noted)
    (("push.recurseSubmodules", "on-demand"), "pushes submodule commits to ANOTHER repo"),
    (("push.recurseSubmodules", "only"), "same, submodules only"),
    (("submodule.recurse", "true"), "implies on-demand (git-config(1))"),
    (("remote.origin.receivepack", "/x/helper"), "runs the configured program on push"),
    (("push.followTags", "true"), "publishes annotated tags alongside"),
    (("push.gpgSign", "true"), "runs gpg.program on push"),
    (("push.gpgSign", "if-asked"), "same, when the server supports it"),
    (("remote.origin.pushurl", "{other}"), "pushes to another local repository"),
    (
        [
            ("remote.origin.url", "https://example.invalid/r.git"),
            ("url.{other}.pushInsteadOf", "https://example.invalid/r.git"),
        ],
        "rewrites the push URL only",
    ),
    (("remote.origin.mirror", "true"), "mirrors every ref"),
]


@pytest.mark.parametrize(
    ("config", "effect"),
    _SIDE_CHANNELS,
    ids=[
        f"{item.rsplit('.', 1)[-1]}={value}"
        for config, _effect in _SIDE_CHANNELS
        for item, value in [config[-1] if isinstance(config, list) else config]
    ],
)
@pytest.mark.parametrize("command", [*_SPELLINGS, "git push", "git push origin feat/x"])
def test_side_channel_config_keeps_the_ask_for_every_spelling(
    monkeypatch, tmp_path, capsys, command: str, config, effect: str
) -> None:
    """The round-1 P1 class, end to end through main(): each config changes what
    a push executes, where it lands, or which refs go with it, and none of them
    may ride a first push's approval — whichever spelling carries the push. The
    two P1 rows were measured in a lab repo before the fix: a colon-refspec push
    under ``on-demand`` created the branch in the SUBMODULE's remote, and
    ``git push origin HEAD`` ran a configured receive-pack helper — both
    auto-allowed."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        command,
        republish=True,
        open_prs=1,
        config=config,
        git_setup=[["tag", "-a", "v9", "-m", "v9", "HEAD"]],
    )
    assert decision == "ask", (effect, reason)
    assert "publishing externally" in reason


@pytest.mark.parametrize("command", ["git push origin", "git push -u origin HEAD"])
@pytest.mark.parametrize(
    "setup",
    [
        # a second URL: the push goes to both, ls-remote asks only the first
        ["remote", "set-url", "--add", "origin", "https://example.invalid/second.git"],
    ],
)
def test_a_remote_with_several_urls_keeps_the_ask(
    monkeypatch, tmp_path, capsys, command, setup
) -> None:
    """Audit finding, MEASURED in a lab: two `url` entries give equal fetch and
    push URL sets and ls-remote hits only the first, while either spelling
    pushes to both."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        command,
        republish=True,
        open_prs=1,
        git_setup=[setup],
    )
    assert decision == "ask", reason


@pytest.mark.parametrize("kind", ["remotes", "branches"])
def test_a_legacy_remote_file_keeps_the_ask(monkeypatch, tmp_path, capsys, kind: str) -> None:
    """A legacy destination/ref mapping is visible in the dry-run ref set."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push origin",
        republish=True,
        open_prs=1,
        legacy_remote_file=kind,
    )
    assert decision == "ask", reason


def test_the_dry_run_verdict_rejects_an_unverified_rewritten_push_url(
    tmp_path, monkeypatch
) -> None:
    repo, remotes = _lab_repo(tmp_path, monkeypatch, republish=False)
    url = "https://example.invalid/r.git"
    other = (remotes / "other.git").resolve().as_uri()
    _git(repo, "push", "--no-verify", other, "HEAD:refs/heads/feat/x")
    _git(repo, "config", f"url.{other}.pushInsteadOf", url)
    seg = _parsed_push_seg(f"git push {url} HEAD")
    verdict = gpg._push_dry_run_verdict(seg, "feat/x", url, cwd=str(repo))
    assert verdict is None


@pytest.mark.parametrize(
    ("rules", "simple"),
    [
        ([], True),
        ([("url.https://example.invalid/b.git.pushInsteadOf", "https://example.invalid/a")], False),
    ],
)
def test_a_raw_url_destination_refuses_any_push_instead_of(tmp_path, monkeypatch, rules, simple):
    """Without a remote section to compare, any pushInsteadOf rule refuses an offline allow."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = _repo(tmp_path)
    for key, value in rules:
        _git(repo, "config", key, value)
    url = "https://example.invalid/a.git"
    rc = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", url],
        capture_output=True,
        timeout=30,
    ).returncode
    assert rc == 2
    assert gpg._push_config_is_simple(url, cwd=str(repo)) is simple


@pytest.mark.parametrize(
    ("command", "allowed"),
    [
        ("git config remote.origin.receivepack /x/helper && git push origin HEAD", False),
        ("git -C . config push.followTags true && git push origin HEAD", False),
        ("git remote set-url --add origin https://example.invalid/b.git && git push", False),
        ("export GIT_CONFIG_COUNT=1; git push origin HEAD", False),
        ("./script.sh && git push origin HEAD", False),
        # allowlisted neighbours keep the relaxation
        ("git add f.txt && git commit -m x && git push origin HEAD", True),
        ("git status && git push -u origin HEAD", True),
        ("git -C . status && git push -u origin HEAD", True),
        # second-audit spellings (MEASURED to write config ahead of the push):
        # a redirect, a process substitution, a neighbour's own -c program
        ("git log -1 --format=x >> .git/config && git push origin HEAD", False),
        ("git status <(git config remote.origin.receivepack /x) && git push origin HEAD", False),
        ('git -c core.fsmonitor="git config push.followTags true" status && git push', False),
        ("cd <(git config push.followTags true) && git push origin HEAD", False),
        ("git fetch --upload-pack=/x/prog origin && git push origin HEAD", False),
        ("git push origin HEAD <(git config push.followTags true)", False),
        # an ALLOWLISTED subcommand whose output is redirected into config:
        # only the re-tokenize equality sees the stripped redirect
        ("git rev-parse --sq-quote x >> .git/config && git push origin HEAD", False),
        # a stripped wrapper on a neighbour
        ("sudo git status && git push origin HEAD", False),
        # a lone push with a redirect keeps the relaxation
        ("git push origin HEAD 2>&1", True),
    ],
)
def test_a_step_that_could_write_config_first_keeps_the_ask(
    monkeypatch, tmp_path, capsys, command: str, allowed: bool
) -> None:
    """Audit finding, MEASURED: the hook reads config before any segment runs,
    so `git config remote.origin.receivepack … && git push origin HEAD` was
    allowed — P1-2 again, one segment earlier."""
    decision, reason = _run(monkeypatch, tmp_path, capsys, command, republish=True, open_prs=1)
    assert decision == ("allow" if allowed else "ask"), reason


@pytest.mark.parametrize(
    ("command", "targets_cur"),
    [
        # `-uo VALUE`: git reads origin as the option value and HEAD as the remote
        ("git push -uo origin HEAD", False),
        ("git push -uo ci.skip origin HEAD", True),
        ("git -C /a -C b push origin HEAD", False),  # -C is cumulative in git
        ("git push origin HEAD  # don't prompt", True),  # apostrophe in a comment
    ],
)
def test_parse_edges_from_the_audit(command: str, targets_cur: bool, monkeypatch) -> None:
    monkeypatch.setattr(gpg, "_push_config_is_simple", lambda *a, **k: True)
    seg = _parsed_push_seg(command)
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=None) is targets_cur


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        (("push.recurseSubmodules", "check"), "allow"),
        (("push.recurseSubmodules", "no"), "allow"),
        (("submodule.recurse", "false"), "allow"),
        (("push.followTags", "false"), "allow"),
        (("push.gpgSign", "false"), "allow"),
        (("push.default", "current"), "allow"),
        (("push.autoSetupRemote", "true"), "allow"),
        # insteadOf rewrites fetch AND push alike, so ls-remote sees the push URL
        (("url.{r}.insteadOf", "https://example.invalid/r.git"), "allow"),
        # a pushurl EQUAL to the url moves nothing
        (("remote.origin.pushurl", "{r}"), "allow"),
    ],
)
def test_config_values_follow_the_static_gate_and_dry_run(
    monkeypatch, tmp_path, capsys, config, expected
) -> None:
    """The static gate takes main's safe recursion set; the remaining values stay safe."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push -u origin HEAD",
        republish=True,
        open_prs=1,
        config=config,
    )
    assert decision == expected, reason


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


@pytest.mark.parametrize(
    ("out", "expected"),
    [
        (
            "To file:///r.git\n \tHEAD:refs/heads/feat/x\tabc\nDone\n",
            (("file:///r.git", ((" ", "HEAD", "refs/heads/feat/x"),)),),
        ),
        (
            "To file:///r.git\n=\tHEAD:refs/heads/feat/x\t[up to date]\nDone\n"
            "To file:///other.git\n*\tHEAD:refs/heads/new\t[new branch]\nDone\n",
            (
                ("file:///r.git", (("=", "HEAD", "refs/heads/feat/x"),)),
                ("file:///other.git", (("*", "HEAD", "refs/heads/new"),)),
            ),
        ),
        (
            "To file:///r.git\n \tHEAD:refs/heads/feat/x\tok\n"
            "*\trefs/tags/v9:refs/tags/v9\t[new tag]\nDone\n",
            (
                (
                    "file:///r.git",
                    (
                        (" ", "HEAD", "refs/heads/feat/x"),
                        ("*", "refs/tags/v9", "refs/tags/v9"),
                    ),
                ),
            ),
        ),
        (
            "To file:///r.git\n-\t:refs/heads/main\t[deleted]\nDone\n",
            (("file:///r.git", (("-", "", "refs/heads/main"),)),),
        ),
        ("To file:///r.git\n \tHEAD:refs/heads/feat/x\tok\n", None),
        ("To file:///r.git\nDone\n", None),
        ("To file:///r.git\n \tHEAD:refs/heads/feat/x\tok\njunk\nDone\n", None),
        (" \tHEAD:refs/heads/feat/x\tok\nDone\n", None),
        ("To \n \tHEAD:refs/heads/feat/x\tok\nDone\n", None),
        ("To file:///r.git\n \tHEAD\tok\nDone\n", None),
        ("", None),
    ],
    ids=[
        "plain",
        "two-blocks",
        "follow-tags",
        "deletion",
        "missing-done",
        "empty-block",
        "junk-line",
        "ref-before-to",
        "empty-url",
        "missing-colon",
        "empty",
    ],
)
def test_parse_push_porcelain(out: str, expected) -> None:
    blocks = gpg._parse_push_porcelain(out)
    if expected is None:
        assert blocks is None
    else:
        assert (
            tuple(
                (block.url, tuple((ref.flag, ref.src, ref.dst) for ref in block.refs))
                for block in blocks
            )
            == expected
        )


@pytest.mark.parametrize(
    ("flag", "kind"),
    [
        (" ", "republish"),
        ("=", "republish"),
        ("*", "first"),
        ("+", None),
        ("-", None),
        ("!", None),
        ("?", None),
    ],
)
def test_dry_run_verdict_flag_matrix(monkeypatch, flag: str, kind: str | None) -> None:
    out = f"To file:///r.git\n{flag}\tHEAD:refs/heads/feat/x\tstatus\nDone\n"
    blocks = gpg._parse_push_porcelain(out)
    if flag == "?":
        assert blocks is None
        blocks = (_dry_run_verdict(flag=flag).block,)
    else:
        assert blocks is not None
    monkeypatch.setattr(gpg, "_push_dest_urls", lambda *a, **k: {"file:///r.git"})
    monkeypatch.setattr(gpg, "_push_transport_precheck", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_push_dry_run", lambda *a, **k: blocks)
    verdict = gpg._push_dry_run_verdict(
        _parsed_push_seg("git push origin HEAD"), "feat/x", "origin"
    )
    assert (verdict.kind if verdict is not None else None) == kind


@pytest.mark.parametrize(
    "out",
    [
        "To file:///r.git\n=\tHEAD:refs/heads/feat/x\tstatus\nDone\n"
        "To file:///r.git\n=\tHEAD:refs/heads/feat/x\tstatus\nDone\n",
        "To file:///r.git\n=\tHEAD:refs/heads/feat/x\tstatus\n"
        "*\trefs/tags/v9:refs/tags/v9\t[new tag]\nDone\n",
    ],
    ids=["two-blocks", "two-refs"],
)
def test_dry_run_verdict_rejects_multiple_blocks_or_refs(monkeypatch, out: str) -> None:
    blocks = gpg._parse_push_porcelain(out)
    assert blocks is not None
    monkeypatch.setattr(gpg, "_push_dest_urls", lambda *a, **k: {"file:///r.git"})
    monkeypatch.setattr(gpg, "_push_transport_precheck", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_push_dry_run", lambda *a, **k: blocks)
    assert (
        gpg._push_dry_run_verdict(_parsed_push_seg("git push origin HEAD"), "feat/x", "origin")
        is None
    )


def test_dry_run_verdict_rejects_a_wrong_porcelain_source(monkeypatch) -> None:
    blocks = (_dry_run_verdict(src="refs/heads/main").block,)
    monkeypatch.setattr(gpg, "_push_dest_urls", lambda *a, **k: {"file:///r.git"})
    monkeypatch.setattr(gpg, "_push_transport_precheck", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_push_dry_run", lambda *a, **k: blocks)
    assert (
        gpg._push_dry_run_verdict(_parsed_push_seg("git push origin HEAD"), "feat/x", "origin")
        is None
    )


@pytest.mark.parametrize(
    ("precheck_url", "porcelain_url", "expected"),
    [
        ("https://host/repo.git", "https://host/repo.git", "republish"),
        ("https://user:pw@host/repo.git", "https://host/repo.git", "republish"),
        ("https://host/repo.git", "https://other/repo.git", None),
    ],
    ids=["exact-match", "userinfo-only-difference", "different-url"],
)
def test_dry_run_verdict_requires_a_matching_porcelain_destination(
    monkeypatch, precheck_url: str, porcelain_url: str, expected: str | None
) -> None:
    default = _dry_run_verdict().block
    block = gpg._DryRunBlock(porcelain_url, default.refs)
    monkeypatch.setattr(gpg, "_repo_selection_env_set", lambda: False)
    monkeypatch.setattr(gpg, "_push_dest_urls", lambda *a, **k: {precheck_url})
    monkeypatch.setattr(gpg, "_push_transport_precheck", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_push_dry_run", lambda *a, **k: (block,))

    verdict = gpg._push_dry_run_verdict(
        _parsed_push_seg("git push origin HEAD"), "feat/x", "origin"
    )

    assert (verdict.kind if verdict is not None else None) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://host/repo.git", "https"),
        ("http://host/repo.git", "http"),
        ("ssh://host/repo.git", "ssh"),
        ("git://host/repo.git", "git"),
        ("file:///tmp/repo.git", "file"),
        ("alice@host:path/repo.git", "ssh"),
        ("/tmp/repo.git", "local"),
        ("./repo.git", "local"),
        ("repo.git", "local"),
        ("ext::sh -c 'touch /tmp/marker'", None),
        ("ftp://host/repo.git", None),
        ("other::helper", None),
    ],
)
def test_push_url_transport_closed_set(url: str, expected: str | None) -> None:
    assert gpg._push_url_transport(url) == expected


@pytest.mark.parametrize("override", ["core", "GIT_SSH_COMMAND", "GIT_SSH"])
def test_ssh_overrides_refuse_before_any_subprocess(monkeypatch, override: str) -> None:
    monkeypatch.setattr(gpg, "_push_dest_urls", lambda *a, **k: {"ssh://host/repo.git"})
    monkeypatch.setattr(
        gpg,
        "_git_config_get",
        lambda base, key, **kwargs: (
            (0, "/usr/bin/ssh") if key == "core.sshCommand" and override == "core" else (1, "")
        ),
    )
    if override != "core":
        monkeypatch.setenv(override, "/usr/bin/ssh-wrapper")
    calls = []
    monkeypatch.setattr(gpg.subprocess, "run", lambda *a, **k: calls.append((a, k)))
    verdict = gpg._push_dry_run_verdict(
        _parsed_push_seg("git push origin HEAD"), "feat/x", "origin"
    )
    assert verdict is None
    assert calls == []


def test_helper_transport_is_rejected_before_dry_run(monkeypatch, tmp_path) -> None:
    marker = tmp_path / "ext-ran"
    ext_url = f"ext::sh -c 'touch {marker}'"
    monkeypatch.setattr(gpg, "_push_dest_urls", lambda *a, **k: {ext_url})
    monkeypatch.setattr(gpg, "_push_dry_run", lambda *a, **k: pytest.fail("dry run was called"))
    verdict = gpg._push_dry_run_verdict(
        _parsed_push_seg("git push origin HEAD"), "feat/x", "origin"
    )
    assert verdict is None
    assert not marker.exists()


def test_real_ext_push_url_is_rejected_before_the_helper_runs(tmp_path, monkeypatch) -> None:
    repo, _remotes = _lab_repo(tmp_path, monkeypatch)
    marker = tmp_path / "ext-ran"
    _git(repo, "config", "remote.origin.pushurl", f"ext::sh -c 'touch {marker}'")
    _git(repo, "config", "protocol.ext.allow", "always")
    assert _real_dry_run_verdict(repo) is None
    assert not marker.exists()


@pytest.mark.parametrize("url", ["https://host/repo.git", "origin"])
def test_remote_vcs_is_rejected_before_dry_run(monkeypatch, url) -> None:
    monkeypatch.setattr(gpg, "_push_dest_urls", lambda *a, **k: {url})
    monkeypatch.setattr(
        gpg,
        "_git_config_get",
        lambda base, key, **kwargs: (0, "custom-helper") if key == "remote.origin.vcs" else (1, ""),
    )
    monkeypatch.setattr(gpg, "_push_dry_run", lambda *a, **k: pytest.fail("dry run was called"))
    verdict = gpg._push_dry_run_verdict(
        _parsed_push_seg("git push origin HEAD"), "feat/x", "origin"
    )
    assert verdict is None


def test_empty_push_url_set_skips_dry_run(monkeypatch) -> None:
    monkeypatch.setattr(gpg, "_push_dest_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "_push_dry_run", lambda *a, **k: pytest.fail("dry run was called"))
    verdict = gpg._push_dry_run_verdict(
        _parsed_push_seg("git push origin HEAD"), "feat/x", "origin"
    )
    assert verdict is None


def _real_dry_run_verdict(
    repo: Path, command: str = "git push origin HEAD", remote: str = "origin"
):
    return gpg._push_dry_run_verdict(_parsed_push_seg(command), "feat/x", remote, cwd=str(repo))


@pytest.mark.parametrize(
    ("case", "flag"),
    [
        ("plain", " "),
        ("up-to-date", "="),
        ("new", "*"),
        ("matching-one-branch", "="),
    ],
)
def test_dry_run_accepts_one_current_branch_ref(
    tmp_path, monkeypatch, case: str, flag: str
) -> None:
    repo, _remotes = _lab_repo(tmp_path, monkeypatch, republish=case != "new")
    command = "git push origin HEAD"
    if case == "plain":
        (repo / "next.txt").write_text("next\n")
        _git(repo, "add", "next.txt")
        _git(repo, "commit", "-qm", "next")
    elif case == "matching-one-branch":
        _git(repo, "config", "push.default", "matching")
        command = "git push"
    blocks = gpg._push_dry_run(gpg._push_ref_positionals(_parsed_push_seg(command).argv), str(repo))
    assert blocks is not None
    assert [(ref.flag, ref.dst) for ref in blocks[0].refs] == [(flag, "refs/heads/feat/x")]
    verdict = _real_dry_run_verdict(repo, command)
    assert verdict is not None
    assert verdict.kind == ("first" if flag == "*" else "republish")


@pytest.mark.parametrize(
    "case",
    [
        "multi-url",
        "follow-tags",
        "upstream",
        "mirror",
        "configured-push-refspec",
        "legacy-remotes-file",
        "missing-remote",
    ],
)
def test_dry_run_rejects_a_remapped_or_multi_ref_push(tmp_path, monkeypatch, case: str) -> None:
    repo, remotes = _lab_repo(tmp_path, monkeypatch)
    command = "git push origin HEAD"
    remote = "origin"
    if case == "multi-url":
        _git(
            repo,
            "remote",
            "set-url",
            "--add",
            "origin",
            (remotes / "second.git").resolve().as_uri(),
        )
    elif case == "follow-tags":
        _git(repo, "config", "push.followTags", "true")
        _git(repo, "tag", "-a", "v9", "-m", "v9", "HEAD")
    elif case == "upstream":
        _git(repo, "push", "--no-verify", "origin", "feat/x:refs/heads/main")
        _git(repo, "fetch", "origin", "main")
        _git(repo, "branch", "--set-upstream-to=origin/main", "feat/x")
        _git(repo, "config", "push.default", "upstream")
        command = "git push"
    elif case == "mirror":
        _git(repo, "config", "remote.origin.mirror", "true")
        command = "git push origin"
    elif case == "configured-push-refspec":
        _git(repo, "config", "remote.origin.push", "HEAD:refs/heads/main")
        command = "git push origin"
    elif case == "legacy-remotes-file":
        _git(repo, "remote", "remove", "origin")
        path = repo / ".git" / "remotes" / "origin"
        path.parent.mkdir(parents=True)
        path.write_text(
            f"URL: {(remotes / 'r.git').resolve().as_uri()}\nPush: HEAD:refs/heads/main\n"
        )
        command = "git push origin"
    else:
        remote = str(tmp_path / "missing.git")
        command = f"git push {remote} HEAD"
    assert _real_dry_run_verdict(repo, command, remote) is None


@pytest.mark.parametrize("family_flag", ["-4", "--ipv4", "-6", "--ipv6"])
def test_dry_run_argv_disables_hooks_drops_set_upstream_and_preserves_family(
    monkeypatch, family_flag: str
) -> None:
    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if "push" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout="To https://example.invalid/r.git\n \tHEAD:refs/heads/feat/x\tok\nDone\n",
                stderr="",
            )
        if "config" in argv:
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")
        return subprocess.CompletedProcess(
            argv, 0, stdout="https://example.invalid/r.git\n", stderr=""
        )

    monkeypatch.setattr(gpg.subprocess, "run", run)
    monkeypatch.setattr(gpg, "_gh_timeout", lambda timeout: 8.5)
    monkeypatch.setattr(gpg, "_repo_selection_env_set", lambda: False)
    verdict = gpg._push_dry_run_verdict(
        _parsed_push_seg(f"git push {family_flag} -u origin HEAD"), "feat/x", "origin"
    )
    assert verdict is not None and verdict.kind == "republish"
    dry_run_argv, dry_run_kwargs = next((argv, kwargs) for argv, kwargs in calls if "push" in argv)
    assert "http.followRedirects=false" in dry_run_argv
    assert "--no-verify" in dry_run_argv
    assert "--recurse-submodules=no" in dry_run_argv
    assert "-u" not in dry_run_argv
    assert family_flag in dry_run_argv
    assert dry_run_kwargs["timeout"] == 8.5
    env = dry_run_kwargs["env"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["SSH_ASKPASS"] == ""
    assert env["LC_ALL"] == "C"


def test_dry_run_does_not_run_pre_push_hook(tmp_path, monkeypatch) -> None:
    repo, _remotes = _lab_repo(tmp_path, monkeypatch)
    marker = tmp_path / "pre-push-ran"
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(f"#!/bin/sh\nprintf x > '{marker}'\n")
    hook.chmod(0o755)
    assert _real_dry_run_verdict(repo) is not None
    assert not marker.exists()


def test_receivepack_helper_is_not_run_before_the_config_gate(tmp_path, monkeypatch) -> None:
    repo, _remotes = _lab_repo(tmp_path, monkeypatch)
    marker = tmp_path / "receivepack-ran"
    helper = tmp_path / "receivepack-helper"
    helper.write_text(f"#!/bin/sh\nprintf x > '{marker}'\nexit 0\n")
    helper.chmod(0o755)
    _git(repo, "config", "remote.origin.receivepack", str(helper))
    seg = _parsed_push_seg("git push origin HEAD")
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin", cwd=str(repo)) is False
    assert not marker.exists()


@pytest.mark.parametrize("scenario", ["receivepack", "pushInsteadOf", "insteadOf", "execpath"])
def test_unapproved_probe_does_not_run_push_side_programs(
    monkeypatch, tmp_path, capsys, scenario: str
) -> None:
    marker = tmp_path / f"{scenario}-ran"
    helper_dir = tmp_path / "helpers"
    helper_dir.mkdir()
    hook_env = {}
    config_text = None
    command = "git push origin HEAD"
    if scenario == "receivepack":
        helper = tmp_path / "receivepack"
        _write_program_stub(helper, marker)
        command = "git push"
        config_text = (
            '[remote "x@y"]\n'
            "    url = {r}\n"
            f"    receivepack = {helper}\n"
            '[branch "feat/x"]\n'
            "    pushRemote = x@y\n"
        )
    elif scenario == "pushInsteadOf":
        helper = tmp_path / "ssh-command"
        _write_program_stub(helper, marker)
        command = "git push https://127.0.0.1:1/original/repo.git HEAD"
        config_text = (
            '[url "ssh://127.0.0.1/rewritten/"]\n'
            "    pushInsteadOf = https://127.0.0.1:1/\n"
        )
        hook_env["GIT_SSH_COMMAND"] = str(helper)
    elif scenario == "insteadOf":
        # The fetch-side rewrite moves a push too, and a raw destination has no
        # remote section for `git remote get-url` to expand.
        helper = tmp_path / "ssh-command"
        _write_program_stub(helper, marker)
        command = "git push https://127.0.0.1:1/original/repo.git HEAD"
        config_text = (
            '[url "ssh://127.0.0.1/rewritten/"]\n'
            "    insteadOf = https://127.0.0.1:1/\n"
        )
        hook_env["GIT_SSH_COMMAND"] = str(helper)
    else:
        helper = helper_dir / "git-receive-pack"
        _write_program_stub(helper, marker)
        hook_env["GIT_EXEC_PATH"] = str(helper_dir)

    probe_argv = []
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        command,
        republish=True,
        open_prs=1,
        config_text=config_text,
        hook_env=hook_env,
        probe_argv=probe_argv,
    )

    assert decision == "ask", reason
    assert probe_argv == []
    assert not marker.exists()


def test_config_gate_short_circuits_before_dry_run(monkeypatch) -> None:
    monkeypatch.setattr(gpg, "_push_config_is_simple", lambda *a, **k: False)
    monkeypatch.setattr(
        gpg, "_push_dry_run_verdict", lambda *a, **k: pytest.fail("dry run was called")
    )
    seg = _parsed_push_seg("git push origin HEAD")
    assert gpg._push_targets_current_branch(seg, "feat/x", "origin") is False
