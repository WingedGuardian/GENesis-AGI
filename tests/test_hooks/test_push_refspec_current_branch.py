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
def test_every_recognised_spelling_passes_the_one_config_predicate(
    command: str, monkeypatch
) -> None:
    """The class the round-1 P1s named: a shape that returned True without the
    config check. With the predicate forced False, NO spelling may be True —
    including the colon form, which previously returned before the check.
    The positive control (predicate True → True) proves each row is a
    recognised shape, so the False is the predicate's doing and not the parse's."""
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
    """MEASURED before the fix: each ``-c`` / ``--config-env`` / assignment-prefix
    row was auto-allowed as a re-push while supplying exactly the config
    ``_push_config_is_simple`` refuses. The config predicate is pinned True so
    the refusal is shown to come from the command's shape."""
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
    ],
)
def test_dry_run_detection_skips_option_values(command: str, dry: bool) -> None:
    assert gpg._push_is_dry_run(_parsed_push_seg(command)) is dry


# ─── end to end through main(): the hygiene checks now run for HEAD ──────────


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ["init", "--quiet", "-b", "main"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["checkout", "--quiet", "-b", "feat/x"],
        ["remote", "add", "origin", "https://example.invalid/r.git"],
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
    config: tuple[str, str] | list[tuple[str, str]] | None = None,
    git_setup: list[list[str]] | None = None,
    legacy_remote_file: str | None = None,
):
    # The config predicate reads real git config; keep the host's global and
    # system files out of it so a developer's own push settings cannot move a row.
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: False)
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: republish)
    monkeypatch.setattr(gpg, "_remote_push_urls", lambda *a, **k: set())
    monkeypatch.setattr(gpg, "push_allowlist", None)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: open_prs)
    repo = _repo(tmp_path)
    configs = [config] if isinstance(config, tuple) else (config or [])
    for pair in configs:
        subprocess.run(["git", "-C", str(repo), "config", *pair], check=True, timeout=30)
    for args in git_setup or []:
        subprocess.run(["git", "-C", str(repo), *args], check=True, timeout=30)
    if legacy_remote_file:
        path = repo / ".git" / legacy_remote_file / "origin"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("URL: https://example.invalid/r.git\nPush: HEAD:refs/heads/main\n")
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


def test_a_fully_qualified_destination_takes_the_same_predicate(
    monkeypatch, tmp_path, capsys
) -> None:
    """`HEAD:refs/heads/<cur>` is immune to the remap itself (MEASURED to update
    `<cur>` under both remapping configs) but not to the side channels that ride
    along with any push, so it goes through the ONE config predicate like every
    other spelling. The price, accepted deliberately: an ask on this rare config."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push origin HEAD:refs/heads/feat/x",
        republish=True,
        open_prs=1,
        config=("remote.origin.push", "refs/heads/feat/x:refs/heads/main"),
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
    (("remote.origin.pushurl", "https://example.invalid/other.git"), "pushes elsewhere"),
    (
        ("url.https://example.invalid/other.git.pushInsteadOf", "https://example.invalid/r.git"),
        "rewrites the push URL only",
    ),
    (("remote.origin.mirror", "true"), "mirrors every ref"),
]


@pytest.mark.parametrize(
    ("config", "effect"),
    _SIDE_CHANNELS,
    ids=[f"{key.rsplit('.', 1)[-1]}={value}" for (key, value), _e in _SIDE_CHANNELS],
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
        monkeypatch, tmp_path, capsys, command, republish=True, open_prs=1, config=config
    )
    assert decision == "ask", (effect, reason)
    assert "publishing externally" in reason


@pytest.mark.parametrize(
    "setup",
    [
        # a second URL: the push goes to both, ls-remote asks only the first
        ["remote", "set-url", "--add", "origin", "https://example.invalid/second.git"],
    ],
)
def test_a_remote_with_several_urls_keeps_the_ask(monkeypatch, tmp_path, capsys, setup) -> None:
    """Audit finding, MEASURED in a lab: two `url` entries give equal fetch and
    push URL sets, ls-remote hit on the first, and `git push origin HEAD`
    created the branch on the second — a first publication the equality alone
    let through."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push -u origin HEAD",
        republish=True,
        open_prs=1,
        git_setup=[setup],
    )
    assert decision == "ask", reason


@pytest.mark.parametrize("kind", ["remotes", "branches"])
def test_a_legacy_remote_file_keeps_the_ask(monkeypatch, tmp_path, capsys, kind: str) -> None:
    """Audit finding, MEASURED: a `.git/remotes/<name>` file with a `Push:` line
    made `git push <name>` create another branch while `remote.<name>.push`
    read as unset. Its mere existence refuses."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push -u origin HEAD",
        republish=True,
        open_prs=1,
        legacy_remote_file=kind,
    )
    assert decision == "ask", reason


@pytest.mark.parametrize(
    ("rules", "simple"),
    [
        ([], True),
        ([("url.https://example.invalid/b.git.pushInsteadOf", "https://example.invalid/a")], False),
    ],
)
def test_a_raw_url_destination_refuses_any_push_instead_of(tmp_path, monkeypatch, rules, simple):
    """The branch of `_push_url_matches_probe` with no remote section to compare
    (`git remote get-url` exits 2): any pushInsteadOf rule refuses, none passes."""
    empty = tmp_path / "empty.gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = _repo(tmp_path)
    for key, value in rules:
        subprocess.run(["git", "-C", str(repo), "config", key, value], check=True, timeout=30)
    url = "https://example.invalid/a.git"
    rc = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", url], capture_output=True, timeout=30
    ).returncode
    assert rc == 2  # the fixture really is the no-remote-section branch
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
        ("./git push origin HEAD", False),
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
    "config",
    [
        ("push.recurseSubmodules", "check"),
        ("push.recurseSubmodules", "no"),
        ("submodule.recurse", "false"),
        ("push.followTags", "false"),
        ("push.gpgSign", "false"),
        ("push.default", "current"),
        ("push.autoSetupRemote", "true"),
        # insteadOf rewrites fetch AND push alike, so ls-remote sees the push URL
        ("url.https://example.invalid/other.git.insteadOf", "https://example.invalid/r.git"),
        # a pushurl EQUAL to the url moves nothing
        ("remote.origin.pushurl", "https://example.invalid/r.git"),
    ],
)
def test_neutral_config_still_rides_the_first_approval(
    monkeypatch, tmp_path, capsys, config
) -> None:
    """The positive control for the table above: values that leave the push
    plain must not prompt, or the predicate is simply refusing everything."""
    decision, reason = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push -u origin HEAD",
        republish=True,
        open_prs=1,
        config=config,
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
