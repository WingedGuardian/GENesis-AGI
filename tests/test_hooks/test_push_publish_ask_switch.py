"""``hooks.asks.push_publish: off`` silences ONE prompt, on ONE repo, and nothing else.

Owner ruling 2026-10-01: an install may turn off the ROUTINE first-publish
approval prompt — the first ``git push`` of the current branch — but only when
the destination is exactly ``https://github.com/<public repo>`` and nothing can
reroute the https transport. The ``gh pr create`` prompt is NOT covered: gh
without a TTY aborts instead of pushing, so that arm is unchanged from main.
These tests are organised around the ways that switch could reach further:

  * **Shape.** Silenced means NO permission decision plus a context note. Never
    an ``allow``: that would approve every other step of a compound command.
  * **Scope.** Any other destination, the ssh/scp forms, a raw URL that a rewrite
    could move, a remote with two URLs, an undeterminable public repo, any
    ``http.*`` config, a proxy/TLS/ssh/config-injection environment variable —
    all keep the ask.
  * **Reach.** Force pushes, the no-open-PR block, close-then-push, a second
    publish in one command, and the dispatched-session deny are unchanged.
  * **Default.** With the key absent, the prompt asks exactly as before.

Since the first-push-requires-PR rule, a FIRST push of the CURRENT branch whose
destination is the configured public repo is DENIED in every mode unless the
same command is ``<plain git push> && gh pr create …`` (ready, not draft, literal
values). That is the client half only, for the shape a hook can read from text;
other push spellings keep their ask, and the guarantee that no branch stays
public unchecked is the server-side leak scan on branch pushes. So on the public repo the shape the switch silences is that chained
form, and the scope tests below drive it with ``CHAIN`` appended; tests whose
destination is NOT the public repo keep the bare push, whose behaviour is
unchanged. Section (f) pins the rule itself.

Real git repos and real remotes are used so the destination resolution (``git
remote get-url --push``, ``insteadOf`` expansion, ``_push_config_is_simple``) runs
for real; only the network probes are stubbed.
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
OTHER = "https://github.com/owner/other"

#: Appended to a first push to the public repo: the one shape that may publish
#: there (``_push_then_pr_create``). Without it the push is denied.
CHAIN = " && gh pr create --title t --body-file b.md"

_BLOCKED_COMMAND_CONFIG = (
    "BLOCKED: this push carries a prefix, wrapper or git config option "
    "(-c / --config-env / VAR=…). Config supplied that way can retarget the push "
    "or make it a force push (e.g. remote.<name>.push=+…, "
    "remote.<name>.pushurl=…) that no flag shows, and the guard cannot read it "
    "before the command runs.\n"
    "Retype it as a plain `git push` (`-C <dir>` is fine)."
)

#: Captured before the autouse fixture stubs it, for the tests that drive it for real.
_REAL_ABSENT = gpg._remote_branch_definitely_absent


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
    (repo / "b.md").write_text("body\n")  # the --body-file CHAIN names
    subprocess.run(["git", "-C", str(repo), "add", "f.txt"], capture_output=True, timeout=30)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "commit.gpgsign=false", "commit", "-qm", "seed"],
        capture_output=True,
        timeout=30,
    )
    return repo


def _run(monkeypatch, tmp_path, capsys, command, git_config=(("remote.origin.url", PUBLIC),)):
    repo = _repo(tmp_path, git_config)
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.chdir(repo)  # gh pr create's remotes are read from where it runs
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
    # The definitive-absence probe is a network call: stubbed to "absent" here,
    # driven for real against a local bare repo in its own tests below.
    monkeypatch.setattr(gpg, "_remote_branch_definitely_absent", lambda *a, **k: True)
    # main() arms a module-global deadline that outlives the call; a later test
    # driving a real probe directly would otherwise inherit an expired budget.
    monkeypatch.setattr(gpg, "_merge_deadline", None)
    for var in gpg._TRANSPORT_ENV + _REPO_ENV:
        monkeypatch.delenv(var, raising=False)


#: Environment variables that select another repository, namespace or git program
#: set. Spelled out here rather than read from the guard, so the test names what it
#: requires instead of agreeing with whatever the guard lists.
_REPO_ENV = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_EXEC_PATH")


@pytest.fixture
def off(monkeypatch):
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off")


@pytest.fixture
def create_publishes(monkeypatch):
    monkeypatch.setattr(gpg, "_pr_create_would_publish", lambda argv: True)


def _assert_silenced(rc, out, err):
    assert rc == 0, (rc, out, err)
    hso = _hso(out)
    assert "permissionDecision" not in hso, out
    assert "permissionDecisionReason" not in hso, out
    assert hso["hookEventName"] == "PreToolUse"
    assert "hooks.asks.push_publish: off" in hso["additionalContext"], out


def _assert_asks(rc, out, err):
    assert rc == 0, (rc, out, err)
    assert _decision(out) == "ask", (out, err)


def _assert_first_push_denied(rc, out, err):
    assert rc == 2, (rc, out, err)
    assert "first push of" in err and "nothing in the command opens its PR" in err, err
    assert not out.strip(), out


# ─── (a) off + every destination public → no decision, a note ────────────────


@pytest.mark.parametrize(
    "command", ["git push -u origin HEAD", "git push origin feat/x", "git push -u origin @"]
)
def test_a_first_push_to_the_public_repo_is_silenced(
    monkeypatch, tmp_path, capsys, off, command
) -> None:
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, command + CHAIN))


@pytest.mark.parametrize("url", [PUBLIC + ".git", PUBLIC + "/", "https://github.com/Owner/Repo"])
def test_every_https_spelling_of_the_public_remote_qualifies(
    monkeypatch, tmp_path, capsys, off, url
) -> None:
    rc, out, err = _run(
        monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN, (("remote.origin.url", url),)
    )
    _assert_silenced(rc, out, err)


@pytest.mark.parametrize(
    "url", ["git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo"]
)
def test_an_ssh_form_origin_always_asks(monkeypatch, tmp_path, capsys, off, url) -> None:
    """The ssh transport is a program; only the https form qualifies."""
    rc, out, err = _run(
        monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN, (("remote.origin.url", url),)
    )
    _assert_asks(rc, out, err)


def test_a_rewrite_that_resolves_to_the_public_repo_is_accepted(
    monkeypatch, tmp_path, capsys, off
) -> None:
    """``git remote get-url --push`` applies insteadOf; the RESULT is what is judged."""
    cfg = (("remote.origin.url", "gh:owner/repo"), ("url.https://github.com/.insteadOf", "gh:"))
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN, cfg))


def test_a_raw_public_url_with_no_rewrite_rule_is_silenced(
    monkeypatch, tmp_path, capsys, off
) -> None:
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, f"git push {PUBLIC} HEAD" + CHAIN))


def test_a_publishing_pr_create_still_asks_when_off(
    monkeypatch, tmp_path, capsys, off, create_publishes
) -> None:
    """The create half was removed: gh without a TTY aborts instead of pushing,
    so the create arm is exactly as on main."""
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "gh pr create --title t --body b")
    _assert_asks(rc, out, err)
    assert "push_publish" not in out


# ─── (b) off + anything not exactly the public repo → still asks / denies ────


@pytest.mark.parametrize(
    ("command", "git_config", "why"),
    [
        ("git push -u origin HEAD", (("remote.origin.url", OTHER),), "origin is another repo"),
        (
            "git push -u origin HEAD",
            (("remote.origin.url", "https://ghe.example.com/owner/repo"),),
            "same name, another host",
        ),
        (
            "git push -u origin HEAD",
            (("remote.origin.url", "https://github.com/x/owner/repo"),),
            "an extra path segment is not an exact match",
        ),
        (
            "git push -u origin HEAD",
            (("remote.origin.url", "https://user@github.com/owner/repo"),),
            "userinfo on https is not an accepted form",
        ),
        (f"git push {OTHER} HEAD", (("remote.origin.url", PUBLIC),), "a raw URL to another repo"),
        (
            f"git push {PUBLIC} HEAD",
            (("remote.origin.url", PUBLIC), (f"url.{OTHER}.insteadOf", PUBLIC)),
            "a raw URL an insteadOf rule could rewrite",
        ),
        (
            "git push -u origin HEAD",
            (("remote.origin.url", PUBLIC), (f"url.{OTHER}.insteadOf", PUBLIC)),
            "insteadOf rewrites the named remote to another repo",
        ),
        (
            "git push -u origin HEAD",
            (("remote.origin.url", PUBLIC), ("--add", "remote.origin.url", OTHER)),
            "a mixed multi-destination remote",
        ),
        (
            "git push -u origin HEAD",
            (("remote.origin.url", PUBLIC), ("remote.origin.pushurl", OTHER)),
            "push URL differs from fetch URL",
        ),
        ("git push -u nosuch HEAD", (("remote.origin.url", PUBLIC),), "unresolvable destination"),
        ("git push -u origin main", (("remote.origin.url", PUBLIC),), "not the current branch"),
        (
            "git push -u origin HEAD",
            (("remote.origin.url", PUBLIC), ("push.followTags", "true")),
            "tags would ride along",
        ),
    ],
)
def test_a_push_that_is_not_exactly_public_still_asks(
    monkeypatch, tmp_path, capsys, off, command, git_config, why
) -> None:
    """Driven in the chained form, so the first-push rule cannot be what refuses:
    every one of these must still ASK rather than be silenced."""
    rc, out, err = _run(monkeypatch, tmp_path, capsys, command + CHAIN, git_config)
    assert rc == 0, (why, rc, out, err)
    assert _decision(out) == "ask", (why, out)


def test_an_undeterminable_public_repo_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "")
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD"))


def test_a_non_public_ask_says_why_the_switch_did_not_apply(
    monkeypatch, tmp_path, capsys, off
) -> None:
    rc, out, err = _run(
        monkeypatch, tmp_path, capsys, "git push -u origin HEAD", (("remote.origin.url", OTHER),)
    )
    _assert_asks(rc, out, err)
    assert (
        "push_publish is off, but this command did not qualify"
        in _hso(out)["permissionDecisionReason"]
    )


def test_a_force_push_to_origin_is_still_blocked(monkeypatch, tmp_path, capsys, off) -> None:
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -f origin HEAD")
    assert rc == 2 and "Force push" in err, (rc, out, err)


def test_a_force_push_to_a_fork_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    rc, out, err = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push -f fork HEAD",
        (("remote.origin.url", PUBLIC), ("remote.fork.url", OTHER)),
    )
    _assert_asks(rc, out, err)
    assert "FORCE" in _hso(out)["permissionDecisionReason"]


# ─── (c) key absent → the prompt asks exactly as before ──────────────────────


def test_the_default_still_asks_for_a_first_push(monkeypatch, tmp_path, capsys) -> None:
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN)
    _assert_asks(rc, out, err)
    assert "push_publish" not in out


def test_the_default_still_asks_for_a_publishing_create(
    monkeypatch, tmp_path, capsys, create_publishes
) -> None:
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "gh pr create --title t"))


def test_push_publish_on_still_asks(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=on")
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN))


def test_a_misconfigured_key_asks_and_says_so(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=maybe")
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN)
    _assert_asks(rc, out, err)
    reason = _hso(out)["permissionDecisionReason"]
    assert "NOTE:" in reason and "push_publish" in reason, reason


# ─── (d) a dispatched session is denied, whatever the switch says ────────────


def test_a_dispatched_push_is_still_denied(monkeypatch, tmp_path, capsys, off) -> None:
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: True)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD")
    assert rc == 2 and "BLOCKED" in err, (rc, out, err)
    assert not out.strip(), out


def test_a_dispatched_publishing_create_is_still_denied(
    monkeypatch, tmp_path, capsys, off, create_publishes
) -> None:
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: True)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "gh pr create --title t")
    assert rc == 2 and "BLOCKED" in err, (rc, out, err)


# ─── (e) compounds and the other push arms are unchanged ─────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "gh pr close 5 && git push -u origin HEAD",
        "git config remote.origin.url https://github.com/owner/other && git push -u origin HEAD",
        "git remote set-url origin https://github.com/owner/other && git push -u origin HEAD",
        "gh pr close 5 && git push -u origin HEAD" + CHAIN,
        "git config remote.origin.url https://github.com/owner/other && git push -u origin HEAD"
        + CHAIN,
        "git remote set-url origin https://github.com/owner/other && git push -u origin HEAD"
        + CHAIN,
    ],
)
def test_a_compound_with_another_operation_is_never_silenced(
    monkeypatch, tmp_path, capsys, off, command
) -> None:
    """A neighbour BEFORE the push breaks the chained shape, so on the public repo
    it is the first-push deny; never the silent note."""
    _assert_first_push_denied(*_run(monkeypatch, tmp_path, capsys, command))


def test_a_second_publish_in_one_command_is_still_blocked(
    monkeypatch, tmp_path, capsys, off
) -> None:
    rc, out, err = _run(
        monkeypatch, tmp_path, capsys, "git push -u origin HEAD && git push origin HEAD"
    )
    assert rc == 2 and "multiple publish" in err, (rc, out, err)


def test_a_create_beside_a_close_still_asks(
    monkeypatch, tmp_path, capsys, off, create_publishes
) -> None:
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "gh pr close 5 && gh pr create --title t")
    _assert_asks(rc, out, err)


def test_the_no_open_pr_block_is_unchanged(monkeypatch, tmp_path, capsys, off) -> None:
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push origin HEAD")
    assert rc == 2 and "NO OPEN PR" in err, (rc, out, err)


def test_close_then_repush_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "gh pr close 5 && git push origin HEAD")
    _assert_asks(rc, out, err)
    assert "CLOSES a pull request" in _hso(out)["permissionDecisionReason"]


# ─── the strict URL matcher ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "slug"),
    [
        ("https://github.com/Owner/Repo", "owner/repo"),
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("git@github.com:owner/repo.git", None),
        ("ssh://git@github.com/owner/repo", None),
        ("https://github.com/x/owner/repo", None),
        ("https://github.com.evil/owner/repo", None),
        ("https://evilgithub.com/owner/repo", None),
        ("http://github.com/owner/repo", None),
        ("https://github.com:443/owner/repo", None),
        ("ssh://git@github.com:22/owner/repo", None),
        ("git@ghe.example.com:owner/repo", None),
        ("/srv/git/owner/repo", None),
        ("file:///github.com/owner/repo", None),
    ],
)
def test_the_strict_matcher(url, slug) -> None:
    assert gpg._strict_github_slug(url) == slug


def test_all_urls_must_match(monkeypatch) -> None:
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "owner/repo")
    assert gpg._all_urls_are_public_repo({PUBLIC}) is True
    assert gpg._all_urls_are_public_repo({PUBLIC, OTHER}) is False
    assert gpg._all_urls_are_public_repo(set()) is False
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "")
    assert gpg._all_urls_are_public_repo({PUBLIC}) is False


@pytest.mark.parametrize("url", [PUBLIC + " ", " " + PUBLIC, PUBLIC + "\n", "\t" + PUBLIC])
def test_the_strict_matcher_refuses_surrounding_whitespace(url) -> None:
    assert gpg._strict_github_slug(url) is None


# ─── round-1 audit: the remote a bare push really reaches (B1) ───────────────


_FORK_BRANCH = (
    ("remote.origin.url", PUBLIC),
    ("remote.fork.url", OTHER),
    ("branch.feat/x.remote", "fork"),
    ("branch.feat/x.merge", "refs/heads/feat/x"),
)


def _run_with_tracking(monkeypatch, tmp_path, capsys, command, cfg, tracking: bool):
    repo = _repo(tmp_path, cfg)
    if tracking:
        subprocess.run(
            ["git", "-C", str(repo), "update-ref", "refs/remotes/fork/feat/x", "HEAD"],
            capture_output=True,
            timeout=30,
            check=True,
        )
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.chdir(repo)
    rc = gpg.main()
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.mark.parametrize("tracking", [False, True], ids=["no-tracking-ref", "tracking-ref"])
def test_a_bare_push_to_branch_remote_fork_still_asks(
    monkeypatch, tmp_path, capsys, off, tracking
) -> None:
    """git pushes a bare `git push` to branch.<cur>.remote even when the
    remote-tracking ref is missing; the guard used to answer "origin" there."""
    rc, out, err = _run_with_tracking(
        monkeypatch, tmp_path, capsys, "git push", _FORK_BRANCH, tracking
    )
    _assert_asks(rc, out, err)


@pytest.mark.parametrize("tracking", [False, True], ids=["no-tracking-ref", "tracking-ref"])
def test_the_repush_allow_checks_branch_remote_not_origin(
    monkeypatch, tmp_path, capsys, tracking
) -> None:
    """The pre-existing re-push allow, same root cause: the branch is on origin
    but NOT on fork, and the bare push goes to fork — a first publication."""
    monkeypatch.setattr(
        gpg, "_push_is_republish", lambda remote, branch, cwd=None: remote == "origin"
    )
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)
    rc, out, err = _run_with_tracking(
        monkeypatch, tmp_path, capsys, "git push", _FORK_BRANCH, tracking
    )
    _assert_asks(rc, out, err)


def test_the_repush_allow_still_works_when_branch_remote_is_origin(
    monkeypatch, tmp_path, capsys
) -> None:
    """Control for the test above: the same setup aimed at origin is a re-push."""
    monkeypatch.setattr(
        gpg, "_push_is_republish", lambda remote, branch, cwd=None: remote == "origin"
    )
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)
    cfg = (("remote.origin.url", PUBLIC), ("branch.feat/x.remote", "origin"))
    rc, out, err = _run_with_tracking(monkeypatch, tmp_path, capsys, "git push", cfg, False)
    assert rc == 0 and _decision(out) == "allow", (rc, out, err)


def test_a_bare_push_with_branch_remote_origin_is_silenced(
    monkeypatch, tmp_path, capsys, off
) -> None:
    cfg = (("remote.origin.url", PUBLIC), ("branch.feat/x.remote", "origin"))
    _assert_silenced(
        *_run_with_tracking(monkeypatch, tmp_path, capsys, "git push" + CHAIN, cfg, False)
    )


def test_a_dot_branch_remote_is_unresolved(monkeypatch, tmp_path, capsys, off) -> None:
    cfg = (("remote.origin.url", PUBLIC), ("branch.feat/x.remote", "."))
    _assert_asks(*_run_with_tracking(monkeypatch, tmp_path, capsys, "git push", cfg, False))


# ─── transport: https-only, no http.* config, no proxy/TLS/config env ────────


@pytest.mark.parametrize(
    ("command", "extra", "why"),
    [
        (
            "git push -u origin HEAD",
            (
                ("http.curloptResolve", "github.com:443:127.0.0.1"),
                ("http.sslVerify", "false"),
            ),
            "DNS pinned to another host with TLS verification off",
        ),
        ("git push -u origin HEAD", (("http.proxy", "http://proxy.invalid:1"),), "http.proxy"),
        (
            "git push -u origin HEAD",
            (("http.https://github.com/.proxy", "http://proxy.invalid:1"),),
            "a per-URL http section",
        ),
        ("git push -u origin HEAD", (("http.extraHeader", "X-A: b"),), "any http.* key at all"),
        (
            f"git push {PUBLIC} HEAD",
            (("http.proxy", "http://proxy.invalid:1"),),
            "raw URL destination, same rule",
        ),
        (
            "git push -u origin HEAD",
            (("remote.origin.proxy", "http://proxy.invalid:1"),),
            "remote proxy",
        ),
        ("git push -u origin HEAD", (("remote.origin.vcs", "helper"),), "a remote helper"),
    ],
)
def test_a_transport_override_in_config_still_asks(
    monkeypatch, tmp_path, capsys, off, command, extra, why
) -> None:
    rc, out, err = _run(
        monkeypatch, tmp_path, capsys, command + CHAIN, (("remote.origin.url", PUBLIC), *extra)
    )
    assert rc == 0, (why, rc, out, err)
    assert _decision(out) == "ask", (why, out)


@pytest.mark.parametrize("var", list(gpg._TRANSPORT_ENV))
def test_a_transport_override_in_the_environment_still_asks(
    monkeypatch, tmp_path, capsys, off, var
) -> None:
    # Values git itself accepts, so the fixture repo can still be built.
    value = {"GIT_CONFIG_COUNT": "0", "GIT_CONFIG_PARAMETERS": "'test.harmless=1'"}.get(var, "1")
    monkeypatch.setenv(var, value)
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN))


def test_a_proxy_plus_no_verify_environment_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN))


def test_an_unreadable_http_config_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    real = gpg._run_git_lines

    def fake(argv):
        if "--get-regexp" in argv and r"^http\." in argv:
            return None
        return real(argv)

    monkeypatch.setattr(gpg, "_run_git_lines", fake)
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN))


def test_a_configured_url_with_trailing_whitespace_still_asks(
    monkeypatch, tmp_path, capsys, off
) -> None:
    rc, out, err = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push -u origin HEAD" + CHAIN,
        (("remote.origin.url", PUBLIC + " "),),
    )
    _assert_asks(rc, out, err)


def test_a_raw_url_with_trailing_whitespace_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, f"git push -u '{PUBLIC} ' HEAD" + CHAIN))


def test_an_unreadable_remote_config_is_unresolved(monkeypatch) -> None:
    """A config read error on the remote precedence chain must not fall through
    to a guess (the upstream or origin): the caller then asks."""
    segs, _ = gpg.analyze_checked("git push")
    monkeypatch.setattr(gpg, "_git_config_get", lambda *a, **k: None)
    monkeypatch.setattr(gpg, "_resolve_push_remote", lambda *a, **k: "origin")
    assert gpg._effective_push_remote(segs[0], "feat/x") is None


# ─── secondary review: definitive absence, and NOTEs on the silenced path ────


def test_an_unconfirmed_absence_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    """`_push_is_republish` answers "not present" on an ls-remote ERROR too. The
    suppression needs a definitive "absent", or it would skip the no-open-PR
    check for an already-public branch whose probe merely failed."""
    monkeypatch.setattr(gpg, "_remote_branch_definitely_absent", lambda *a, **k: False)
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN))


def test_the_probe_is_asked_about_the_resolved_url_and_branch(
    monkeypatch, tmp_path, capsys, off
) -> None:
    seen = []
    monkeypatch.setattr(
        gpg, "_remote_branch_definitely_absent", lambda url, branch, cwd: seen.append((url, branch)) or True
    )
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN))
    assert seen == [(PUBLIC, "feat/x")], seen


def test_a_present_branch_keeps_the_republish_path(monkeypatch, tmp_path, capsys, off) -> None:
    """Probe says present (republish): the existing re-push logic decides, and
    the no-open-PR block still fires on the public repo."""
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    probe = []
    monkeypatch.setattr(gpg, "_remote_branch_definitely_absent", lambda *a, **k: probe.append(a) or True)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push origin HEAD")
    assert rc == 2 and "NO OPEN PR" in err, (rc, out, err)
    assert probe == [], "the suppression probe must not run on the re-push path"


def _bare(tmp_path, with_branch: bool) -> str:
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "--quiet", "--bare", str(bare)], check=True, timeout=30)
    if with_branch:
        src = _repo(tmp_path / "src")
        subprocess.run(
            ["git", "-C", str(src), "push", "--quiet", str(bare), "HEAD:refs/heads/feat/x"],
            check=True, capture_output=True, timeout=30,
        )
    return str(bare)


def test_the_real_probe_reports_absent(tmp_path) -> None:
    assert _REAL_ABSENT(_bare(tmp_path, False), "feat/x", None) is True


def test_the_real_probe_reports_present_as_not_absent(tmp_path) -> None:
    assert _REAL_ABSENT(_bare(tmp_path, True), "feat/x", None) is False


def test_the_real_probe_treats_an_error_as_not_absent(tmp_path) -> None:
    assert _REAL_ABSENT(str(tmp_path / "nope.git"), "feat/x", None) is False


def test_the_real_probe_treats_a_timeout_as_not_absent(monkeypatch, tmp_path) -> None:
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1)

    monkeypatch.setattr(gpg.subprocess, "run", boom)
    assert _REAL_ABSENT("https://github.com/owner/repo", "feat/x", None) is False


def test_policy_notes_ride_the_silenced_note(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=off,force_push=off")
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN)
    _assert_silenced(rc, out, err)
    ctx = _hso(out)["additionalContext"]
    assert "NOTE:" in ctx and "force_push" in ctx, ctx


# ─── round 2 (terminal): only a single plain `git push` is ever silenced ─────


def _hook_file(repo: Path, name: str, body: str) -> None:
    hook = repo / ".git" / "hooks" / name
    hook.write_text("#!/bin/sh\n" + body + "\n")
    hook.chmod(0o755)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # `cd` into a directory that does not exist: the push's directory is
        # unknown, so the branch cannot be resolved and the push ASKS.
        ("cd sub && git push -u origin HEAD", "ask"),
        ("cd sub | git push -u origin HEAD", "ask"),
        ("cd sub & git push -u origin HEAD", "ask"),
        ("cd sub\ngit push -u origin HEAD", "ask"),
        ("cd sub; git push -u origin HEAD", "ask"),
        # The current branch resolves, but the command is not the chained shape:
        # the first-push deny.
        ("git push -u origin HEAD & git rev-parse HEAD", "deny"),
        ("git push -u origin HEAD > out.txt", "deny"),
        ("git push -u origin HEAD && true", "deny"),
        ("git -C . push -u origin HEAD", "deny"),
        ("git -P push -u origin HEAD", "deny"),
        ("git --no-pager push -u origin HEAD", "deny"),
        ("\\git push -u origin HEAD", "deny"),
    ],
)
def test_anything_but_a_single_plain_push_is_never_silenced(
    monkeypatch, tmp_path, capsys, off, command, expected
) -> None:
    """Never the silent note, and exactly one pinned outcome per shape. Each shape
    is also driven with ``CHAIN`` appended — the extra segment must not turn it
    into the silenced shape, nor change the outcome."""
    for i, cmd in enumerate((command, command + CHAIN)):
        rc, out, err = _run(monkeypatch, tmp_path / str(i), capsys, cmd)
        if expected == "ask":
            _assert_asks(rc, out, err)
        else:
            _assert_first_push_denied(rc, out, err)


@pytest.mark.parametrize(
    "command",
    [
        "(git push -u origin HEAD)",
        "env git push -u origin HEAD",
        "command git push -u origin HEAD",
        "X=1 git push -u origin HEAD",
    ],
)
def test_command_borne_config_push_blocks(monkeypatch, tmp_path, capsys, off, command) -> None:
    rc, out, err = _run(monkeypatch, tmp_path, capsys, command)
    assert rc == 2, (command, rc, out, err)
    assert _BLOCKED_COMMAND_CONFIG in err, (command, out, err)


def test_a_cdpath_cd_before_the_push_is_never_silenced(monkeypatch, tmp_path, capsys, off) -> None:
    """Codex P1: with CDPATH set, `cd sub` can land in an unrelated repo while the
    guard resolves `sub` against the payload cwd."""
    other = _repo(tmp_path / "cdpath" / "sub", (("remote.origin.url", OTHER),))
    monkeypatch.setenv("CDPATH", str(other.parent.parent))
    rc, out, err = _run(
        monkeypatch, tmp_path / "main", capsys, "cd sub && git push -u origin HEAD" + CHAIN
    )
    # The `cd` target is not resolvable from the payload cwd, so the push asks.
    _assert_asks(rc, out, err)


@pytest.mark.parametrize(
    ("command", "setup"),
    [
        ("git status && git push -u origin HEAD", "fsmonitor"),
        ("git add -A && git push -u origin HEAD", "clean-filter"),
        ("git commit --allow-empty -m x && git push -u origin HEAD", "hook-pushurl"),
        ("git commit --allow-empty -m x && git push -u origin HEAD", "hook-checkout"),
    ],
)
def test_a_neighbour_that_runs_code_before_the_push_is_never_silenced(
    monkeypatch, tmp_path, capsys, off, command, setup
) -> None:
    """Class audit: each "inert" neighbour can run configured code that changes
    the push after the hook judged it. Driven WITH a chained PR: the neighbour
    still breaks the chained shape, so on the public repo it is the first-push
    deny — never silenced."""
    command += CHAIN
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC), ("commit.gpgsign", "false")))
    if setup == "fsmonitor":
        subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", "true"], check=True)
    elif setup == "clean-filter":
        subprocess.run(["git", "-C", str(repo), "config", "filter.z.clean", "cat"], check=True)
    elif setup == "hook-pushurl":
        _hook_file(repo, "post-commit", f"git config remote.origin.pushurl {OTHER}")
    else:
        _hook_file(repo, "post-commit", "git checkout -q -b other")
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.chdir(repo)
    rc = gpg.main()
    out = capsys.readouterr()
    _assert_first_push_denied(rc, out.out, out.err)


def test_a_dash_C_through_a_symlink_is_never_silenced(monkeypatch, tmp_path, capsys, off) -> None:
    """git follows `lnk` before applying `..`; os.path.normpath does not."""
    import os

    other = _repo(tmp_path / "other", (("remote.origin.url", OTHER),))
    (other / "sub").mkdir()
    repo = _repo(tmp_path / "main")
    os.symlink(other / "sub", repo / "lnk")
    for command in ("git -C lnk/.. push -u origin HEAD", "git -C lnk/.. push -u origin HEAD" + CHAIN):
        payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(repo)}
        monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.chdir(repo)
        rc = gpg.main()
        out = capsys.readouterr()
        # Measured outcome: an ask, never the silent note. The first-push deny does
        # not fire, because this `-C` spelling is not read as a plain push of the
        # public checkout's current branch.
        _assert_asks(rc, out.out, out.err)


def test_the_probe_refuses_redirects_and_prompts(monkeypatch) -> None:
    """Codex P2: a renamed repository redirects; following it would vouch for a
    different repository. And a credential prompt must never hang the hook."""
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["env"] = list(args), kwargs.get("env") or {}
        return subprocess.CompletedProcess(args, 128, "", "redirect refused")

    monkeypatch.setattr(gpg.subprocess, "run", fake_run)
    assert _REAL_ABSENT(PUBLIC, "feat/x", None) is False
    args, env = seen["args"], seen["env"]
    assert args[:3] == ["git", "-c", "http.followRedirects=false"], args
    assert args[-3:] == ["--heads", PUBLIC, "refs/heads/feat/x"] and "--exit-code" in args
    assert env.get("GIT_TERMINAL_PROMPT") == "0", env
    assert env.get("GIT_ASKPASS") == "" and env.get("SSH_ASKPASS") == "", env


def test_the_segment_count_is_checked_on_its_own() -> None:
    """Belt to the metacharacter check: a second parsed segment refuses even if
    no separator character survived into the text (a parse this guard did not
    foresee), and so does a push segment that is not the one handed in."""
    segs, _ = gpg.analyze_checked("git push -u origin HEAD")
    push = segs[0]
    assert gpg._is_single_plain_push(segs, push, "git push -u origin HEAD") is True
    assert gpg._is_single_plain_push([push, push], push, "git push -u origin HEAD") is False
    other, _ = gpg.analyze_checked("git push -u origin HEAD")
    assert gpg._is_single_plain_push(other, push, "git push -u origin HEAD") is False


@pytest.mark.parametrize(
    "command", ["git push o* HEAD", "git push origin? HEAD", "git push [o]rigin HEAD"]
)
def test_a_glob_in_the_push_keeps_the_prompt(command: str) -> None:
    """A remote name written into config can contain glob characters, so a glob
    could expand to a different word than the one judged: never silenced."""
    segs, _ = gpg.analyze_checked(command)
    assert gpg._is_single_plain_push(segs, segs[0], command) is False


# ─── (f) a first push to the public repo must bring its PR with it ───────────
#
# The re-push block only catches a PR-less branch on its NEXT push; a session
# that stops after one push used to leave it public with no PR. So a first push
# of the CURRENT branch to the public repo is refused unless the same command is
# exactly `<plain git push> && gh pr create …` with a ready create and literal
# values. Client half only: what gh resolves at run time (base repo, base
# branch, commits ahead) and other push spellings are not modelled here — the
# server-side leak scan on branch pushes is the guarantee.


@pytest.fixture(params=["default", "off", "on"])
def any_mode(request, monkeypatch):
    if request.param != "default":
        monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", f"push_publish={request.param}")
    return request.param


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin HEAD",
        "git push origin feat/x",
        "git push",
        f"git push {PUBLIC} HEAD",
    ],
)
def test_a_bare_first_push_to_the_public_repo_is_denied_in_every_mode(
    monkeypatch, tmp_path, capsys, any_mode, command
) -> None:
    cfg = (("remote.origin.url", PUBLIC), ("branch.feat/x.remote", "origin"))
    _assert_first_push_denied(*_run(monkeypatch, tmp_path, capsys, command, cfg))


def test_the_deny_is_written_for_the_agent(monkeypatch, tmp_path, capsys) -> None:
    """Names the hazard, gives the exact passing form, and says where unready
    work goes instead."""
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD")
    _assert_first_push_denied(rc, out, err)
    assert "PUBLIC with NO PR" in err and "server-side leak scan" in err, err
    assert 'git push -u origin HEAD && gh pr create --title "..." --body-file <file>' in err, err
    assert "stays committed locally, unpushed" in err, err
    assert "--draft/-d" in err, err


def test_the_chained_form_is_silenced_when_off(
    monkeypatch, tmp_path, capsys, off, create_publishes
) -> None:
    """``create_publishes`` makes the would-publish probe answer True, as it does
    for real before the push runs. The chained create is exempt from it — it runs
    only after the push succeeded — so the command is still silenced."""
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN)
    _assert_silenced(rc, out, err)
    assert "first push of 'feat/x'" in _hso(out)["additionalContext"]


@pytest.mark.parametrize("mode", ["default", "on"])
def test_the_chained_form_asks_when_the_switch_is_not_off(
    monkeypatch, tmp_path, capsys, create_publishes, mode
) -> None:
    if mode == "on":
        monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=on")
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD" + CHAIN)
    _assert_asks(rc, out, err)
    assert "git push needs your approval" in _hso(out)["permissionDecisionReason"]


@pytest.mark.parametrize(
    "create",
    [
        "gh pr create -t t -b b",
        "gh pr create --title=t --body-file=b.md",
        "gh pr create -tt -Fb.md",
        "gh pr create --fill",
        "gh pr create -f --no-maintainer-edit",
        "gh pr create --fill-first",
        "gh pr create --title 'fix(hooks): a && b; c | d' --body-file b.md",
        "gh pr create --title -d --body-file b.md",
        "gh pr create -t x -b y # trailing note",
        "gh pr create --fill-verbose",
        "gh pr create -f --fill",
        "gh pr create -t x -b ''",
    ],
)
def test_every_ready_create_spelling_qualifies(
    monkeypatch, tmp_path, capsys, off, create
) -> None:
    """``--title -d`` is a TITLE: gh consumes a value flag's next token whole."""
    _assert_silenced(
        *_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD && " + create)
    )


@pytest.mark.parametrize(
    ("command", "why"),
    [
        # Every create below carries a title AND a body (`-t t -b b`), so the only
        # thing that can refuse it is the defect named in `why`.
        ("git push -u origin HEAD && gh pr create --draft -t t -b b", "--draft"),
        ("git push -u origin HEAD && gh pr create -d -t t -b b", "-d"),
        ("git push -u origin HEAD && gh pr create --draft=true -t t -b b", "--draft=true"),
        ("git push -u origin HEAD && gh pr create -fd", "a bundle carrying -d"),
        ("git push -u origin HEAD && gh pr create -t t -b b --web", "--web opens no PR"),
        ("git push -u origin HEAD && gh pr create -t t -b b --dry-run", "--dry-run opens no PR"),
        ("git push -u origin HEAD && gh pr create -t t -b b --head other", "another head"),
        ("git push -u origin HEAD && gh pr create -t t -b b -R a/b", "another repo"),
        ("git push -u origin HEAD && gh pr create -t t -b b --base dev", "a non-default base"),
        ("git push -u origin HEAD && gh pr create -t t -b b extra", "a positional"),
        ("git push -u origin HEAD && gh pr create -b b -t", "a value flag with no value"),
        ("git push -u origin HEAD || gh pr create -t t -b b", "|| runs the create only on failure"),
        ("git push -u origin HEAD ; gh pr create -t t -b b", "; ignores the push's result"),
        ("git push -u origin HEAD & gh pr create -t t -b b", "& runs them concurrently"),
        ("git push -u origin HEAD\ngh pr create -t t -b b", "a newline is ;"),
        ("git push -u origin HEAD # note && gh pr create -t t -b b", "bash comments out the create"),
        ('git push -u origin HEAD && gh pr create -t "$(cat t)" -b b', "a run-time substitution"),
        ("git push -u origin HEAD && gh pr create -t $T -b b", "a run-time expansion"),
        ("git push -u origin HEAD && gh pr create -t `cat t` -b b", "a backtick substitution"),
        ("git push -u origin HEAD && gh pr create -t * -b b", "a glob (*)"),
        ("git push -u origin HEAD && gh pr create -t 'a?' -b b", "a glob (?), even quoted"),
        ("git push -u origin HEAD && gh pr create -t [ab] -b b", "a glob ([)"),
        ("git push -u origin HEAD && gh pr create -t {a,b} -b b", "a brace expansion"),
        ("git push -u origin HEAD && gh pr create -t t -F ~/b.md", "a tilde expansion"),
        ('git push -u origin HEAD && gh pr create -t t -b "$(cat x)"', "a quoted substitution"),
        (
            "git push -u origin HEAD && gh pr create -t t -b b && gh pr create -t u -b b",
            "a third step",
        ),
        ("git push -u origin HEAD && { gh pr create -t t -b b; }", "a group"),
        ("git push -u origin HEAD && GH_REPO=a/b gh pr create -t t -b b", "a prefix on the create"),
        ("git push -u origin HEAD && command gh pr create -t t -b b", "a wrapper on the create"),
        ("git push -u origin HEAD && gh pr create -t t -b b > out.txt", "a redirect"),
        ("git push -u origin HEAD && gh pr create -t t -b b &", "a backgrounded chain"),
        ("git push -u origin HEAD && gh pr view", "not a create"),
        ("git commit -m x && git push -u origin HEAD && gh pr create -t t -b b", "a step before"),
    ],
)
def test_anything_but_the_exact_chained_shape_is_denied(
    monkeypatch, tmp_path, capsys, any_mode, command, why
) -> None:
    rc, out, err = _run(monkeypatch, tmp_path, capsys, command)
    assert rc == 2, (why, rc, out, err)
    assert "first push of" in err, (why, err)


def test_a_first_push_off_the_public_repo_is_unchanged(monkeypatch, tmp_path, capsys) -> None:
    """Requirement 3: a push to any other repository asks, bare or chained."""
    cfg = (("remote.origin.url", OTHER),)
    for i, cmd in enumerate(("git push -u origin HEAD", "git push -u origin HEAD" + CHAIN)):
        rc, out, err = _run(monkeypatch, tmp_path / str(i), capsys, cmd, cfg)
        _assert_asks(rc, out, err)


def test_a_first_push_with_no_public_repo_declared_is_unchanged(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setenv("_TEST_CANONICAL_PUBLIC_REPO", "")
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD"))


def test_a_dry_run_first_push_publishes_nothing_and_is_not_denied(
    monkeypatch, tmp_path, capsys
) -> None:
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push --dry-run -u origin HEAD"))


def test_a_repush_is_unaffected(monkeypatch, tmp_path, capsys) -> None:
    """Re-push paths are untouched: with an open PR the silent allow, without one
    the existing re-push block (its own text, not the first-push one)."""
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 1)
    rc, out, err = _run(monkeypatch, tmp_path / "a", capsys, "git push origin HEAD")
    assert rc == 0 and _decision(out) == "allow", (rc, out, err)
    monkeypatch.setattr(gpg, "_open_pr_count_for_branch", lambda *a, **k: 0)
    rc, out, err = _run(monkeypatch, tmp_path / "b", capsys, "git push origin HEAD")
    assert rc == 2 and "re-push to 'feat/x'" in err and "first push of" not in err, err


@pytest.mark.parametrize("command", ["git push -u origin HEAD", "git push -u origin HEAD" + CHAIN])
def test_a_dispatched_first_push_keeps_its_own_deny(
    monkeypatch, tmp_path, capsys, command
) -> None:
    """The dispatched path is untouched: it is refused before the first-push rule
    is reached, with its own message, chained or not."""
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: True)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, command)
    assert rc == 2 and "Autonomous/dispatched sessions cannot push" in err, err
    assert "first push of" not in err, err


def test_a_hard_block_elsewhere_in_the_command_speaks_first(monkeypatch, tmp_path, capsys) -> None:
    """The deny is DEFERRED like the re-push block, so an inline hard block later
    in the guard still decides with its own diagnostic."""
    rc, out, err = _run(
        monkeypatch, tmp_path, capsys, "git push -u origin HEAD; git commit --no-verify -m x"
    )
    assert rc == 2 and "--no-verify" in err, err
    assert "first push of" not in err, err


def test_the_chained_create_alone_skips_the_would_publish_probe(
    monkeypatch, tmp_path, capsys, off
) -> None:
    """Only the create chained after the push is exempt; the probe still runs for
    a standalone create."""
    probed = []
    monkeypatch.setattr(gpg, "_pr_create_would_publish", lambda argv: probed.append(argv) or True)
    _assert_silenced(*_run(monkeypatch, tmp_path / "a", capsys, "git push -u origin HEAD" + CHAIN))
    assert probed == [], probed
    _assert_asks(*_run(monkeypatch, tmp_path / "b", capsys, "gh pr create --title t"))
    assert len(probed) == 1, probed


@pytest.mark.parametrize(
    ("argv", "ok"),
    [
        # gh refuses these with no terminal (MEASURED, gh 2.101): no title+body
        # and no fill flag.
        (["gh", "pr", "create"], False),
        (["gh", "pr", "create", "--title", "t"], False),
        (["gh", "pr", "create", "-t", "t", "-l", "bug"], False),
        (["gh", "pr", "create", "--body", "b"], False),
        (["gh", "pr", "create", "--body-file", "f"], False),
        (["gh", "pr", "create", "--title", "t", "--body-file", "f"], True),
        (["gh", "pr", "create", "-tt", "-Ff"], True),
        (["gh", "pr", "create", "-t", "t", "-b", ""], True),
        (["gh", "pr", "create", "-t", "--draft", "-b", "b"], True),
        (["gh", "pr", "create", "--fill"], True),
        (["gh", "pr", "create", "-f"], True),
        (["gh", "pr", "create", "-f", "--fill"], True),
        (["gh", "pr", "create", "--fill-first"], True),
        (["gh", "pr", "create", "--fill-verbose"], True),
        (["gh", "pr", "create", "--fill", "--fill-first"], False),
        (["gh", "pr", "create", "-f", "--fill-verbose"], False),
        (["gh", "pr", "create", "--title", "", "--body", "b"], False),
        (["gh", "pr", "create", "-t", "x", "-t", " ", "-b", "b"], False),
        (["gh", "pr", "create", "--title", "", "--fill"], False),
        (["gh", "pr", "create", "-t", "t", "--body-file", "missing.md"], False),
        (["gh", "pr", "create", "-t", "t", "-F", "-"], False),
        (["gh", "pr", "create", "-t", "t", "-F", "adir"], False),
        # gh's parser drops one `=` after a short flag: `-F=f` opens `f`, and
        # `-t=` is read as an empty title (Codex P2, comment 4179903846).
        (["gh", "pr", "create", "-t", "t", "-F=f"], True),
        (["gh", "pr", "create", "-t=", "-b", "b"], False),
        # GitHub rejects titles over 256 characters (Codex P2, comment 4249185535).
        (["gh", "pr", "create", "-t", "x" * 256, "-b", "b"], True),
        (["gh", "pr", "create", "-t", "x" * 257, "-b", "b"], False),
        (["gh", "pr", "create", "--title=" + "x" * 257, "--fill"], False),
        # gh: "specify only one of `--body` or `--body-file`" (review SF1).
        (["gh", "pr", "create", "-t", "t", "--body", "x", "--body-file", "f"], False),
        (["gh", "pr", "create", "-t", "t", "-b", "x", "-F", "f"], False),
        # GitHub's 65,536-character body limit, literal body (review SF2).
        (["gh", "pr", "create", "-t", "t", "-b", "x" * 65536], True),
        (["gh", "pr", "create", "-t", "t", "-b", "x" * 65537], False),
        (["gh", "pr", "create", "-t", "t", "--body=" + "é" * 65537], False),
        (["gh", "pr", "create", "--draft"], False),
        (["gh", "pr", "create", "-d"], False),
        (["gh", "pr", "create", "--draft=false"], False),
        (["gh", "pr", "create", "-fd"], False),
        (["gh", "pr", "create", "--fill=true"], False),
        (["gh", "pr", "create", "--title"], False),
        (["gh", "pr", "create", "--", "-t", "x"], False),
        (["gh", "pr", "view"], False),
    ],
)
def test_the_create_option_allowlist(tmp_path, argv, ok) -> None:
    (tmp_path / "f").write_text("body\n")
    (tmp_path / "adir").mkdir()
    assert gpg._create_opts_open_a_ready_pr(argv, str(tmp_path)) is ok


@pytest.mark.parametrize("kind", ["unreadable", "not-utf8", "too-large"])
def test_a_body_file_gh_could_not_read_does_not_qualify(tmp_path, kind) -> None:
    """Codex P2 (comment 4179903835): existing is not enough. A regular file gh
    cannot open or read fails the create AFTER the push, so the hook must
    actually read it: openable, at most 1 MiB, UTF-8."""
    import os

    body = tmp_path / "f"
    if kind == "unreadable":
        body.write_text("body\n")
        body.chmod(0)
        try:
            body.read_bytes()
        except OSError:
            pass
        else:
            pytest.skip("this user can read a mode-000 file (root)")
    elif kind == "not-utf8":
        body.write_bytes(b"\xff\xfe body \x80\n")
    else:
        body.write_bytes(b"x" * (gpg._CREATE_BODY_FILE_MAX + 1))
    try:
        argv = ["gh", "pr", "create", "-t", "t", "-F", "f"]
        assert gpg._create_opts_open_a_ready_pr(argv, str(tmp_path)) is False
    finally:
        os.chmod(body, 0o600)


@pytest.mark.parametrize(
    ("text", "ok"),
    [
        ("x" * 65536, True),
        ("x" * 65537, False),
        # Characters, not bytes: 65,536 two-byte characters still fit.
        ("é" * 65536, True),
        ("é" * 65537, False),
    ],
)
def test_a_body_file_is_held_to_githubs_character_limit(tmp_path, text, ok) -> None:
    """Review SF2: GitHub rejects a PR body over 65,536 characters, after the
    push. The byte bound only limits the read; the decoded length decides."""
    (tmp_path / "f").write_text(text, encoding="utf-8")
    argv = ["gh", "pr", "create", "-t", "t", "-F", "f"]
    assert gpg._create_opts_open_a_ready_pr(argv, str(tmp_path)) is ok


def test_a_body_file_is_opened_once_and_typed_on_the_descriptor(monkeypatch, tmp_path) -> None:
    """Review N6: the type is checked with fstat on the descriptor that is then
    read — never a stat by path followed by a separate open."""
    import os

    (tmp_path / "f").write_text("body\n")
    calls: list[str] = []
    real_open, real_fstat = os.open, os.fstat

    def spy_open(path, flags, *a, **k):
        calls.append("open")
        assert flags & os.O_NONBLOCK and flags & os.O_CLOEXEC, flags
        return real_open(path, flags, *a, **k)

    def spy_fstat(fd):
        calls.append("fstat")
        return real_fstat(fd)

    def no_stat(*a, **k):
        raise AssertionError("stat by path")

    monkeypatch.setattr(gpg.os, "open", spy_open)
    monkeypatch.setattr(gpg.os, "fstat", spy_fstat)
    monkeypatch.setattr(gpg.os, "stat", no_stat)
    assert gpg._body_file_is_readable(str(tmp_path / "f")) is True
    assert calls == ["open", "fstat"], calls


def test_an_unreadable_body_file_does_not_carry_a_first_push(
    monkeypatch, tmp_path, capsys, off
) -> None:
    """End to end: the chained shape with a mode-000 body file is the bare-push
    deny, while the same shape spelled `-F=b.md` (readable) is silenced."""
    import os

    rc, out, err = _run(
        monkeypatch, tmp_path / "ok", capsys, "git push -u origin HEAD && gh pr create -t t -F=b.md"
    )
    _assert_silenced(rc, out, err)
    repo = _repo(tmp_path / "bad", (("remote.origin.url", PUBLIC),))
    os.chmod(repo / "b.md", 0)
    try:
        if os.access(repo / "b.md", os.R_OK):
            pytest.skip("this user can read a mode-000 file (root)")
        payload = {"tool_name": "Bash", "tool_input": {"command": "git push -u origin HEAD" + CHAIN}}
        payload["cwd"] = str(repo)
        monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
        monkeypatch.chdir(repo)
        rc = gpg.main()
        cap = capsys.readouterr()
        _assert_first_push_denied(rc, cap.out, cap.err)
    finally:
        os.chmod(repo / "b.md", 0o600)


def test_a_body_file_needs_a_known_directory() -> None:
    """With no resolvable cwd the file cannot be checked, so the create does not
    qualify; the same create without a body file is unaffected."""
    assert gpg._create_opts_open_a_ready_pr(["gh", "pr", "create", "-t", "t", "-F", "f"]) is False
    assert gpg._create_opts_open_a_ready_pr(["gh", "pr", "create", "-t", "t", "-b", "b"]) is True


# ─── finding 1, end to end: a create gh would refuse never carries a push ─────


@pytest.mark.parametrize(
    ("create", "why"),
    [
        ("gh pr create", "no title, no body, no fill"),
        ("gh pr create --title x", "a title alone"),
        ("gh pr create -t x -l bug", "a title and metadata, still no body"),
        ("gh pr create --body y", "a body alone"),
        ("gh pr create --title x --body-file missing.md", "a body file that does not exist"),
        ("gh pr create --title x --body-file -", "a body read from stdin"),
        ("gh pr create --title '' --body y", "a blank title"),
        ("gh pr create --fill --fill-first", "two fill flags"),
    ],
)
def test_a_create_gh_would_refuse_does_not_carry_a_first_push(
    monkeypatch, tmp_path, capsys, any_mode, create_publishes, create, why
) -> None:
    """The push runs first; a create that then fails leaves the branch public
    with no PR — exactly the state the chained shape exists to rule out."""
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD && " + create)
    assert rc == 2, (why, rc, out, err)
    assert "first push of" in err, (why, err)


def test_the_deny_names_what_a_create_needs(
    monkeypatch, tmp_path, capsys, create_publishes
) -> None:
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD && gh pr create -t x")
    _assert_first_push_denied(rc, out, err)
    assert "BOTH a non-empty title (--title/-t) AND a body" in err, err
    assert "exactly one of --fill/-f, --fill-first, --fill-verbose" in err, err


# ─── finding 4: a dry-run push does not exempt the create after it ───────────


def test_a_dry_run_chain_keeps_the_create_probe(monkeypatch, tmp_path, capsys, off) -> None:
    """After `git push -n` the branch is still not on the remote, so the create
    may push it itself: its would-publish probe must run, and it asks."""
    probed = []
    monkeypatch.setattr(gpg, "_pr_create_would_publish", lambda argv: probed.append(argv) or True)
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -n -u origin HEAD" + CHAIN)
    _assert_asks(rc, out, err)
    assert len(probed) == 1, probed
    assert "gh pr create would push" in _hso(out)["permissionDecisionReason"]


# ─── (h) review round: the remaining ways to end up public with no PR ─────────


@pytest.mark.parametrize(
    "create",
    [
        "gh pr create -t t -b b --label x",
        "gh pr create -t t -b b --label=x",
        "gh pr create -t t -b b -l x",
        "gh pr create -t t -b b -lx",
        "gh pr create -t t -b b --assignee @me",
        "gh pr create -t t -b b -a @me",
        "gh pr create -t t -b b --reviewer someone",
        "gh pr create -t t -b b -r someone",
        "gh pr create -t t -b b --milestone m1",
        "gh pr create -t t -b b -m m1",
        "gh pr create -t t -b b --project p",
        "gh pr create -t t -b b -p p",
        "gh pr create --fill -l bug",
    ],
)
def test_a_create_carrying_metadata_does_not_carry_a_first_push(
    monkeypatch, tmp_path, capsys, any_mode, create_publishes, create
) -> None:
    """gh resolves labels, assignees, reviewers, milestones and projects BEFORE it
    creates the PR and fails on an unknown one — after the push has published the
    branch. So none of them may ride the chained create; they go on afterwards
    with `gh pr edit`."""
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD && " + create)
    _assert_first_push_denied(rc, out, err)
    assert "gh pr edit" in err, err


@pytest.mark.parametrize(
    "argv",
    [
        ["gh", "pr", "create", "--fill", "--label", "x"],
        ["gh", "pr", "create", "-f", "-lbug"],
        ["gh", "pr", "create", "-t", "t", "-b", "b", "--assignee=@me"],
        ["gh", "pr", "create", "-t", "t", "-b", "b", "-p", "p"],
    ],
)
def test_the_create_allowlist_refuses_metadata(argv) -> None:
    assert gpg._create_opts_open_a_ready_pr(argv, None) is False


# ─── harmless spellings: `git -C <sibling worktree>` and an output-only pipe ──
#
# Measured 2026-10-05: dispatched agents published with
# `git -C <worktree> push -u origin HEAD 2>&1 | tail -2`, and both the `-C` and
# the pipe disqualified the suppression, so every one of them prompted. Each
# accepted spelling below is silenced; each near-miss beside it still asks.


def _real(path: Path) -> Path:
    import os

    return Path(os.path.realpath(path))


def _with_worktree(tmp_path) -> tuple[Path, Path]:
    """A repo whose origin is the public repo, plus a linked worktree of it on its
    own branch. Real paths, so a symlinked tmp dir cannot fail the realpath rule."""
    repo = _repo(_real(tmp_path) / "main", (("remote.origin.url", PUBLIC),))
    wt = _real(tmp_path) / "wt"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-q", "-b", "feat/wt", str(wt)],
        capture_output=True,
        timeout=30,
        check=True,
    )
    return repo, wt


def _run_at(monkeypatch, capsys, command: str, cwd, extra: dict | None = None):
    payload = {"tool_name": "Bash", "tool_input": {"command": command}}
    if cwd is not None:
        payload["cwd"] = str(cwd)
    payload.update(extra or {})
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    if cwd is not None:
        monkeypatch.chdir(cwd)
    rc = gpg.main()
    out = capsys.readouterr()
    return rc, out.out, out.err


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin HEAD 2>&1",
        "git push -u origin HEAD 2>&1 | tail -2",
        "git push -u origin HEAD | tail -n 5",
        "git push -u origin HEAD 2>&1 | tail -n 20 | head -3",
        "git push -u origin HEAD 2>&1|head -1",
    ],
)
def test_an_output_only_suffix_is_silenced(monkeypatch, tmp_path, capsys, off, command) -> None:
    # Flipped for first-push-requires-PR: a lone first push to the public repo is now refused.
    _assert_first_push_denied(*_run(monkeypatch, tmp_path / "lone", capsys, command))
    # The suffix allowance itself still stands where no deny applies: a dry run.
    dry = command.replace("git push ", "git push --dry-run ", 1)
    _assert_silenced(*_run(monkeypatch, tmp_path / "dry", capsys, dry))


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin HEAD | tee out.txt",
        "git push -u origin HEAD 2>&1 | tee out.txt",
        "git push -u origin HEAD | sh",
        "git push -u origin HEAD | bash",
        "git push -u origin HEAD | xargs echo",
        "git push -u origin HEAD | tail -f",
        "git push -u origin HEAD | tail -2 out.txt",
        "git push -u origin HEAD | tail +2",
        "git push -u origin HEAD | head -c 5",
        "git push -u origin HEAD |& tail -2",
        "git push -u origin HEAD || tail -2",
        "git push -u origin HEAD 2> err.txt",
        "git push -u origin HEAD 2>&1 > out.txt",
        "git push -u origin HEAD > out.txt 2>&1",
        "git push -u origin HEAD 2>&1 | tail -2 > out.txt",
        "git push -u origin HEAD 2>&1 | tail -2 && true",
        "git push -u origin HEAD 2>&1 | tail -2; true",
        "git push -u origin HEAD 2>&1 | tail -2 &",
        "git push -u origin HEAD 2>&1 | tail -2 # note",
        "git push -u origin HEAD # 2>&1 | tail -2",
        "git push -u origin HEAD 2>&1 | tail -2 | sh",
        "git push -u origin HEAD 2>&1 | tail <(git config -l)",
        "tail -2 | git push -u origin HEAD",
        "cd sub && git push -u origin HEAD 2>&1 | tail -2",
        "git -c x.y=z push -u origin HEAD 2>&1 | tail -2",
    ],
)
def test_a_pipe_or_redirect_that_can_do_more_than_filter_output_asks(
    monkeypatch, tmp_path, capsys, off, command
) -> None:
    # A lone first push to the public repo is now refused outright; the dry-run
    # spelling (no deny) keeps this a test of the suffix rule itself.
    dry = command.replace("git push ", "git push --dry-run ", 1)
    for i, cmd in enumerate((command, dry)):
        rc, out, err = _run(monkeypatch, tmp_path / str(i), capsys, cmd)
        if rc == 2:
            continue  # refused outright by another gate: not silenced, which is the point
        assert _decision(out) == "ask", (cmd, out, err)


#: Since first-push-requires-PR a LONE first push to the public repo is refused,
#: and the chained form allows neither `-C` nor an output suffix. The `-C` rule
#: therefore still decides only where no deny applies — a dry run — so the
#: tests below drive the dry-run spelling to keep judging it.
DRY = "push --dry-run -u origin HEAD"


@pytest.mark.parametrize("suffix", ["", " 2>&1 | tail -2", " | tail -n 5"])
def test_dash_C_to_a_sibling_worktree_is_silenced(monkeypatch, tmp_path, capsys, off, suffix):
    """The measured incident shape: the session sits in the main checkout and
    publishes a linked worktree's branch with `-C`."""
    repo, wt = _with_worktree(tmp_path)
    # Flipped for first-push-requires-PR: the lone first push is now refused.
    command = f"git -C {wt} push -u origin HEAD{suffix}"
    _assert_first_push_denied(*_run_at(monkeypatch, capsys, command, repo))
    rc, out, err = _run_at(monkeypatch, capsys, f"git -C {wt} {DRY}{suffix}", repo)
    _assert_silenced(rc, out, err)
    assert "feat/wt" in _hso(out)["additionalContext"], out  # judged in the worktree


def test_dash_C_to_the_sessions_own_checkout_is_silenced(monkeypatch, tmp_path, capsys, off):
    repo, _wt = _with_worktree(tmp_path)
    # Flipped for first-push-requires-PR: the lone first push is now refused.
    command = f"git -C {repo} push -u origin HEAD"
    _assert_first_push_denied(*_run_at(monkeypatch, capsys, command, repo))
    _assert_silenced(*_run_at(monkeypatch, capsys, f"git -C {repo} {DRY}", repo))


def test_dash_C_from_a_worktree_back_to_the_main_checkout_is_silenced(
    monkeypatch, tmp_path, capsys, off
):
    repo, wt = _with_worktree(tmp_path)
    # Flipped for first-push-requires-PR: the lone first push is now refused.
    command = f"git -C {repo} push -u origin HEAD"
    _assert_first_push_denied(*_run_at(monkeypatch, capsys, command, wt))
    _assert_silenced(*_run_at(monkeypatch, capsys, f"git -C {repo} {DRY}", wt))


def test_dash_C_never_carries_the_chained_create(monkeypatch, tmp_path, capsys, off) -> None:
    """The create runs in the session's directory, not the `-C` one, so it would
    open the PR for whatever branch is checked out THERE: `-C` disqualifies the
    chained shape even when it names a valid sibling worktree."""
    repo, wt = _with_worktree(tmp_path)  # repo carries the b.md that CHAIN names
    command = f"git -C {wt} push -u origin HEAD" + CHAIN
    _assert_first_push_denied(*_run_at(monkeypatch, capsys, command, repo))


def test_dash_C_through_a_symlink_to_a_sibling_worktree_asks(monkeypatch, tmp_path, capsys, off):
    import os

    repo, wt = _with_worktree(tmp_path)
    link = _real(tmp_path) / "lnk"
    os.symlink(wt, link)
    _assert_asks(*_run_at(monkeypatch, capsys, f"git -C {link} {DRY}", repo))


def test_dash_C_through_a_symlinked_parent_asks(monkeypatch, tmp_path, capsys, off):
    import os

    repo, wt = _with_worktree(tmp_path)
    alias = _real(tmp_path) / "alias"
    os.symlink(wt.parent, alias)
    command = f"git -C {alias / wt.name} {DRY}"
    _assert_asks(*_run_at(monkeypatch, capsys, command, repo))


def test_dash_C_to_another_repository_asks(monkeypatch, tmp_path, capsys, off):
    """Same public origin, but a DIFFERENT repository: its hooks and config are
    not the ones this session's checkout runs under."""
    repo, _wt = _with_worktree(tmp_path)
    other = _repo(_real(tmp_path) / "elsewhere", (("remote.origin.url", PUBLIC),))
    _assert_asks(*_run_at(monkeypatch, capsys, f"git -C {other} {DRY}", repo))


@pytest.mark.parametrize(
    "spelling",
    [
        "{wt}/",  # trailing slash: not its own realpath
        "{wt}/../wt",  # `..` in the word
        "{wt}/sub",  # a subdirectory, not the top level
        ".",  # relative
        "wt",  # relative
        "{wt} -C {wt}",  # two -C
        "{wt} -c x.y=z",  # another global option after -C
        "{wt} --no-pager",  # even a harmless global option
    ],
)
def test_a_dash_C_that_is_not_exactly_a_worktree_top_asks(
    monkeypatch, tmp_path, capsys, off, spelling
):
    repo, wt = _with_worktree(tmp_path)
    (wt / "sub").mkdir()
    command = f"git -C {spelling.format(wt=wt)} {DRY}"
    rc, out, err = _run_at(monkeypatch, capsys, command, repo)
    if rc == 2:
        return
    assert _decision(out) == "ask", (command, out, err)


def test_dash_C_without_a_known_session_cwd_asks(monkeypatch, tmp_path, capsys, off):
    """No payload cwd: there is nothing to compare the repository against."""
    repo, wt = _with_worktree(tmp_path)
    monkeypatch.chdir(repo)
    _assert_asks(*_run_at(monkeypatch, capsys, f"git -C {wt} {DRY}", None))


def test_dash_C_from_a_session_outside_any_repository_asks(monkeypatch, tmp_path, capsys, off):
    _repo_dir, wt = _with_worktree(tmp_path)
    elsewhere = _real(tmp_path) / "plain"
    elsewhere.mkdir()
    _assert_asks(*_run_at(monkeypatch, capsys, f"git -C {wt} {DRY}", elsewhere))


@pytest.mark.parametrize("var", _REPO_ENV)
def test_a_repository_selecting_variable_in_the_environment_asks(
    monkeypatch, tmp_path, capsys, off, var
) -> None:
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    # Set only for the guard's own decision, after the fixture repo exists: a
    # repository variable in the environment would otherwise redirect the setup.
    monkeypatch.setenv(var, str(repo / ".git") if var != "GIT_NAMESPACE" else "ns")
    # Dry run: a lone first push to the public repo is refused before this matters.
    _assert_asks(*_run_at(monkeypatch, capsys, "git push --dry-run -u origin HEAD", repo))


@pytest.mark.parametrize("var", ["GIT_DIR", "GIT_COMMON_DIR"])
def test_the_dash_C_check_refuses_a_repository_variable_on_its_own(
    monkeypatch, tmp_path, var
) -> None:
    """Security review: with GIT_DIR in the environment every `git -C` probe answers
    about the overriding repository, so an UNRELATED directory compared equal to
    the session's repository. The helper must refuse by itself, whatever order its
    caller checks the environment in."""
    repo, wt = _with_worktree(tmp_path)
    unrelated = _real(tmp_path) / "unrelated"
    unrelated.mkdir()
    for target, expected_clean in ((wt, True), (unrelated, False)):
        command = f"git -C {target} push -u origin HEAD"
        segs, _ = gpg.analyze_checked(command)
        kw = {"hook_cwd": str(repo), "push_cwd": str(target)}
        assert gpg._is_single_plain_push(segs, segs[0], command, **kw) is expected_clean
        monkeypatch.setenv(var, str(repo / ".git"))
        assert gpg._is_single_plain_push(segs, segs[0], command, **kw) is False, target
        monkeypatch.delenv(var)


def test_the_dash_C_judgement_runs_against_the_named_directory(tmp_path) -> None:
    """Belt: the directory every scope check read (``push_cwd``) must BE the `-C`
    word. A valid sibling worktree judged from a different directory refuses."""
    repo, wt = _with_worktree(tmp_path)
    command = f"git -C {wt} push -u origin HEAD"
    segs, _ = gpg.analyze_checked(command)
    kw = {"hook_cwd": str(repo)}
    assert gpg._is_single_plain_push(segs, segs[0], command, push_cwd=str(wt), **kw) is True
    assert gpg._is_single_plain_push(segs, segs[0], command, push_cwd=str(repo), **kw) is False
    assert gpg._is_single_plain_push(segs, segs[0], command, push_cwd=None, **kw) is False


def test_an_output_filter_must_be_its_own_parsed_segment() -> None:
    """The text split and the parse must agree: a filter stage the parser did not
    produce as a plain top-level segment refuses."""
    command = "git push -u origin HEAD | tail -2"
    segs, _ = gpg.analyze_checked(command)
    assert gpg._is_single_plain_push(segs, segs[0], command) is True
    assert gpg._is_single_plain_push(segs[:1], segs[0], command) is False
    other, _ = gpg.analyze_checked("git push -u origin HEAD | head -2")
    assert gpg._is_single_plain_push([segs[0], other[1]], segs[0], command) is False


# ─── a subagent never publishes: refused, never asked ─────────────────────────

_SUBAGENT = {"agent_id": "a0123456789abcdef", "agent_type": "general-purpose"}


@pytest.mark.parametrize("policy", ["push_publish=off", ""])
@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin HEAD",
        "git push -u origin HEAD 2>&1 | tail -2",
        "git push origin feat/x",
        "bash -c 'git push -u origin HEAD'",
        "gh pr create --title t --body b",
        "git push -u origin HEAD && gh pr create --title t --body b",
        # Told it does not publish, not first told to split the command.
        "git push -u origin HEAD && git push origin HEAD",
        # Policy, not a scope judgement: a dry run is refused too.
        "git push --dry-run origin HEAD",
    ],
)
def test_a_subagent_push_or_create_is_denied(monkeypatch, tmp_path, capsys, policy, command):
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", policy)
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, _SUBAGENT)
    assert rc == 2, (command, rc, out, err)
    assert "only the main session publishes" in err, err
    assert "git rev-parse HEAD" in err and "main session" in err, err
    assert not out.strip(), out  # no prompt, no note: the owner sees nothing


def test_a_subagent_repush_of_a_published_branch_is_denied(monkeypatch, tmp_path, capsys) -> None:
    """The re-push relaxation would ALLOW this from the main thread; a subagent
    still does not publish."""
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, "git push origin feat/x", repo, _SUBAGENT)
    assert rc == 2 and "only the main session publishes" in err, (rc, out, err)


def test_a_dispatched_subagent_is_still_denied(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: True)
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, "git push -u origin HEAD", repo, _SUBAGENT)
    assert rc == 2 and "only the main session publishes" in err, (rc, out, err)


@pytest.mark.parametrize(
    "command",
    [
        "gh pr review 5 \\\n  --approve",
        "gh issue close \\\n  7",
        "gh api -X POST repos/o/r/issues/5/comments \\\n  -f body=x",
        "gh pr view 5 \\\n  --json title",  # a read too: its text cannot be read
    ],
)
def test_a_subagent_line_continued_gh_command_is_refused(
    monkeypatch, tmp_path, capsys, command
) -> None:
    """A continuation withholds the segments, so the per-segment check sees nothing;
    without this a continued gh write ran silently from a subagent."""
    segs, blind = gpg.analyze_checked(command)
    assert segs == [] and blind is not None and blind.bounds_induced, (segs, blind)
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, _SUBAGENT)
    assert rc == 2 and "one line" in err, (command, rc, out, err)
    assert not out.strip(), out


@pytest.mark.parametrize(
    "command",
    [
        "gh pr create --help",
        "gh pr create -h",
        "gh pr comment 5 --body '@codex review' --help",
    ],
)
@pytest.mark.parametrize("extra", [{}, _SUBAGENT], ids=["main", "subagent"])
def test_a_gh_help_request_raises_no_prompt(monkeypatch, tmp_path, capsys, extra, command) -> None:
    """Help prints text and runs nothing, so neither the create arm nor the review
    budget may put a prompt in front of the owner for it."""
    # No budget module: a review request reaching the lookup reads "unknown" and
    # asks, so only skipping it for help keeps the owner out of it. No network.
    monkeypatch.setattr(gpg, "_review_budget", None)
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, extra)
    assert rc == 0 and "permissionDecision" not in out, (command, rc, out, err)


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"agent_type": "security-reviewer"},  # a main session started with --agent
        {"agent_id": ""},
        {"agent_id": None},
        {"agent_id": 7},
    ],
    ids=["no-agent-fields", "agent-type-only", "empty-id", "null-id", "non-string-id"],
)
def test_a_main_thread_push_is_unaffected(monkeypatch, tmp_path, capsys, off, extra) -> None:
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    # Flipped for first-push-requires-PR: a lone first push is refused (by that rule, not this one).
    rc, out, err = _run_at(monkeypatch, capsys, "git push -u origin HEAD", repo, extra)
    _assert_first_push_denied(rc, out, err)
    assert "only the main session publishes" not in err
    rc, out, err = _run_at(monkeypatch, capsys, "git push -u origin HEAD" + CHAIN, repo, extra)
    _assert_silenced(rc, out, err)
    assert "only the main session publishes" not in err


def test_a_main_thread_push_still_asks_by_default(monkeypatch, tmp_path, capsys) -> None:
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    extra = {"agent_type": "x"}
    # Flipped for first-push-requires-PR: the lone first push is refused; the chained form asks.
    _assert_first_push_denied(
        *_run_at(monkeypatch, capsys, "git push -u origin HEAD", repo, extra)
    )
    _assert_asks(*_run_at(monkeypatch, capsys, "git push -u origin HEAD" + CHAIN, repo, extra))


@pytest.mark.parametrize(
    "command", ["git status", "git commit --allow-empty -m x", "gh pr view 5", "git log -1"]
)
def test_a_subagent_that_does_not_publish_is_not_refused(monkeypatch, tmp_path, capsys, command):
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, _SUBAGENT)
    assert "only the main session publishes" not in err, (command, err)
    assert rc == 0, (command, rc, out, err)


# ─── every GitHub write, not only push/create (owner decision 2026-10-05) ─────
#
# Each command below writes to GitHub. From a subagent it is refused with the
# hand-back message; from the main thread the SAME command is never refused by
# this rule (it may still meet the guard's other gates, which are not this test's
# subject). The classifier is driven through `main()`; the read list beside it
# pins what a subagent keeps.

_GITHUB_WRITES = [
    # The incident: a subagent's review request reached the owner as a prompt.
    "gh pr comment 5 --body '@codex review'",
    "gh pr comment 5 --body 'an ordinary comment'",
    "gh pr review 5 --approve",
    "gh pr review 5 --comment -b looks-fine",
    "gh pr edit 5 --add-label x",
    "gh pr ready 5",
    "gh pr ready 5 --undo",
    "gh pr reopen 5",
    "gh pr close 5",
    "gh pr merge 5 --squash --admin",
    "gh pr lock 5",
    "gh pr update-branch 5",
    "gh -R owner/repo pr comment 5 -b x",
    "gh issue create --title t --body b",
    "gh issue comment 7 --body x",
    "gh issue edit 7 --add-label x",
    "gh issue close 7",
    "gh issue reopen 7",
    "gh issue develop 7",
    "gh release create v1",
    "gh release upload v1 a.tgz",
    "gh repo edit --description x",
    "gh repo fork",
    "gh repo sync",
    "gh label create bug",
    "gh gist create f.txt",
    "gh workflow run ci.yml",
    "gh workflow enable ci.yml",
    "gh run rerun 123",
    "gh run cancel 123",
    "gh cache delete key",
    "gh secret set NAME --body x",
    "gh variable set NAME --body x",
    "gh project item-add 1 --owner o --url u",
    "gh codespace create",  # a group in neither table: unknown effect
    "gh my-alias 5",  # a gh alias or extension: unknown effect
    # REST writes: an explicit method, or a parameter (gh then defaults to POST).
    "gh api -X POST repos/o/r/issues/5/comments -f body=x",
    "gh api repos/o/r/pulls/5/comments -f body=fixed -F in_reply_to=123",
    "gh api --method PATCH repos/o/r/pulls/5 -f state=closed",
    "gh api -XDELETE repos/o/r/git/refs/heads/x",
    "gh -X PUT api repos/o/r/pulls/5/merge",
    "gh api repos/o/r/issues --input body.json",
    # GraphQL: a mutation, or a query the command line does not show.
    "gh api graphql -f query='mutation { addComment(input: {}) { clientMutationId } }'",
    "gh api graphql -F query=@q.graphql",
    "gh api graphql --input q.json",
    # git publishing plumbing.
    "git send-pack https://github.com/owner/repo HEAD",
    "git http-push https://github.com/owner/repo HEAD",
    "git lfs push origin feat/x",
    # Nested, and chained behind a read.
    "bash -c 'gh pr comment 5 --body x'",
    "gh pr view 5 && gh pr comment 5 --body x",
    # A flag before the verb: gh's lookup swallows the next word as its value, so
    # the word that looks like a read verb is not the command that runs (MEASURED
    # with `--help` appended, gh 2.101: each resolves to the write named last).
    "gh pr --admin view merge 5",
    "gh pr --yes view comment 5 -b x",
    "gh release --draft list create --notes x",
    "gh label --force list create",
    "gh repo --clone list fork",
    "gh secret --no-store list set NAME",
    "gh --foo search pr comment 5 -b x",
    # GraphQL whose text the command line does not show.
    'gh api graphql -f query="$(cat q.graphql)"',
    'gh api graphql -f query="$Q"',
    'gh api graphql -f query="query { viewer { login } } $(cat more.graphql)"',
    "gh api graphql -f query='query { a }`cat more.graphql`'",
    "gh api graphql -f owner=o",
    # A method override header, whatever method it names.
    "gh api repos/o/r/pulls/5 -H 'X-HTTP-Method-Override: DELETE'",
    # Running an extension, and git's other pushers.
    "gh extension exec my-ext",
    "gh ext exec my-ext",
    "git subtree push --prefix=docs origin gh-pages",
    # subtree takes its options before the verb too, with a separate value.
    "git subtree --prefix docs push origin gh-pages",
    "git subtree -P docs --squash push origin gh-pages",
    # A mutation after a string, a comment, or an unterminated string stays a write.
    r"""gh api graphql -f query='query { a(x: "\" ") } mutation { b }'""",
    "gh api graphql -f query='query { a } # c\nmutation { b }'",
    """gh api graphql -f query='query { a(x: "mutation) }'""",
    "gh api graphql -f query='query { a(x: \"x\nmutation { b } \") }'",
    '''gh api graphql -f query='query { a(x: """ \\""" """) } mutation { b }\'''',
    # `--help` as an unknown flag's value is not help: gh runs the command.
    "gh release create v1 --notes --help",
    "gh label create x --description --help",
    # `--version` belongs to the root command; after a group it swallows a word.
    "gh pr --version view comment 5 -b x",
    # GraphQL treats a comma as whitespace.
    "gh api graphql -f query='query{viewer{login}},mutation{addStar(input:{}){x}}'",
    # Extension and alias names may carry capitals and underscores.
    "gh My_Ext run",
    "gh foo_bar",
]

_GITHUB_READS = [
    "gh pr view 5",
    "gh pr list --state open",
    "gh pr diff 5",
    "gh pr checks 5",
    "gh pr status",
    "gh pr checkout 5",
    "gh pr",
    "gh issue view 7",
    "gh issue list",
    "gh run view 123 --log",
    "gh run watch 123",
    "gh workflow list",
    "gh release view v1",
    "gh repo view owner/repo",
    "gh search prs foo",
    "gh status",
    "gh auth status",
    "gh --version",
    "gh api repos/o/r/pulls/5/comments",
    "gh api repos/o/r/pulls/5/comments --paginate --jq '.[].body'",
    "gh api -X GET search/issues -f q=repo:o/r",
    "gh api graphql -f query='query { viewer { login } }'",
    "gh api graphql -f query='query($n: Int!) { viewer { repositories(first: $n) { totalCount } } }' -F n=5",
    'gh api graphql -f query=\'query { m: __type(name: "Mutation") { name } }\'',
    # The word `mutation` inside a string value or a comment is not an operation.
    """gh api graphql -f 'query=query { search(query: "label:mutation bug", type: ISSUE) { issueCount } }'""",
    '''gh api graphql -f query='query { search(query: """a mutation""", type: ISSUE) { issueCount } }\'''',
    r"""gh api graphql -f query='query { a(x: "\"") b(y: "mutation") }'""",
    '''gh api graphql -f query='query { a(x: """ \\""" mutation """) }\'''',
    "gh api graphql -f query='query { viewer { login } } # no mutation here\n'",
    "git fetch origin",
    "git ls-remote origin",
    # Help lookups print text and run nothing (the largest wrongly-refused group).
    "gh pr merge 5 --squash --help",
    "gh pr ready --help",
    "gh label create -h",
    "gh api --help",
    # gh's own aliases for reads.
    "gh pr ls",
    "gh pr co 5",
    "gh issue ls",
    "gh ext list",
    "gh rs list",
    "gh repo gitignore list",
    "gh -R owner/repo pr view 5",
    "gh pr --repo=owner/repo view 5",
    "gh pr --help view comment",  # help IS declared at the group level
    # No verb: every word after the group is a flag value, nothing is hidden.
    "gh status -o myorg",
    # gh's root alias for `gh pr checkout`.
    "gh co 5",
    # `mutation` as an alias or a variable name is inside the brackets, not an
    # operation keyword at the document's top level.
    "gh api graphql -f query='query { mutation: viewer { login } }'",
    "gh api graphql -f query='query($mutation: Int) { viewer { login } }' -F mutation=1",
    "gh api graphql -f query='{ viewer { login } }'",
    "gh api graphql -f query='fragment F on User { login } query { viewer { ...F } }'",
    # Local git work, and git's network reads.
    "git pull --ff-only",
    "git clone https://github.com/o/r",
    "git remote -v",
    "git remote add up https://github.com/o/r",
    "git submodule update --init",
    "git lfs ls-files",
    "git lfs locks",
    "git rebase origin/main",
    "git -C /tmp/x worktree list",
    "git bisect start",
    "git --version",
    "git fetch --unshallow origin",
    "git fetch -u origin",
    "git grep -n foo",
    "gh ssh-key list",
    "gh gpg-key list",
    "gh -R o/r pr view 5",
]

#: Not on the read list, so refused from a subagent: the owner ruling of
#: 2026-10-10 replaced the denylist of writes with an allowlist of reads. Each
#: entry is a write the denylist missed, or a command whose effect the guard
#: cannot read.
_NOT_ON_THE_READ_LIST = [
    # Codex P1: auth flows change GitHub state (an uploaded SSH key, new scopes).
    "gh auth login --git-protocol ssh",
    "gh auth refresh --scopes repo",
    "gh auth logout",
    "gh auth setup-git",
    # Codex P2: LFS lock verbs write the LFS server.
    "git lfs lock big.bin",
    "git lfs unlock big.bin",
    "git lfs pre-push origin",
    # An unknown gh subcommand, group, and `gh api` method.
    "gh pr frobnicate 5",
    "gh newgroup thing",
    "gh api -X FOO repos/o/r",
    "gh api --method PROPFIND repos/o/r",
    "gh api -X GET repos/o/r --input body.json",
    # A GraphQL subscription is not a query either.
    "gh api graphql -f query='subscription { x }'",
    "gh api graphql -f query='fragment F on User { login } mutation { x { ...F } }'",
    "gh api graphql -f query='query { a } }{ mutation { b }'",
    # Not reads, and not network reads.
    "gh completion -s bash",
    "gh browse -n",
    # git: an alias or unknown subcommand, a launcher, a subtree.
    "git co main",
    "git frobnicate",
    "git subtree split --prefix docs",
    "git for-each-repo --config=maintenance.repo push",
    "git submodule foreach 'git push'",
    "git bisect run make test",
    "git rebase -x 'git push origin HEAD' main",
    "git rebase --exec='git push' main",
    "git rebase --ex 'git push' main",
    "git -c alias.p=push p origin HEAD",
    'git "$SUB" origin HEAD',
    # A flag before the group swallows the next word, so the real command is
    # the one after it (architect F1; MEASURED with gh 2.101).
    "gh --approve https://github.com/o/r/pull/5 pr review",
    "gh --yes o/r repo delete",
    "gh --admin co pr merge",
    "gh --yes api repo delete",
    # git options passed through gh, and git options that run a program.
    "gh repo clone o/r -- --upload-pack='sh -c x'",
    "gh co 5 -- --upload-pack=x",
    "git fetch --upload-pack='sh -c x' .",
    "git fetch --upload-p=x .",
    "git pull --upload-pack=x origin main",
    "git ls-remote -u x .",
    "git clone -c core.sshCommand=x https://github.com/o/r",
    "git clone --template=/t https://github.com/o/r",
    "git grep -Ocat foo",
    "git grep --open-files-in-pager=cat foo",
    "git fetch-pack --exec=x .",
    "git archive --remote=x --exec=y HEAD",
]

#: The program is an expansion, so the parse cannot say whether it is git or gh;
#: refused when the command names either (architect F2).
_PROGRAM_IS_AN_EXPANSION = [
    "G=gh; $G pr comment 5 -b x",
    "G=git; $G push origin main",
    "$(echo gh) pr comment 5 -b x",
]


def test_a_value_that_reads_as_help_is_not_help() -> None:
    """`--body --help` posts the text "--help": the flag's value is not a flag."""
    segs, _ = gpg.analyze_checked("gh pr comment 5 --body --help")
    assert gpg._subagent_refusal(segs[0]) == "gh pr comment"


def test_heredoc_text_that_starts_with_gh_is_not_a_gh_call() -> None:
    """A here-doc line `gh = Fake()` parses as a segment whose group is `=`."""
    command = "cat >> t.py <<'EOF'\ngh = FakeGh()\nEOF"
    segs, _ = gpg.analyze_checked(command)
    assert any(s.exe == "gh" for s in segs), "fixture no longer yields a gh segment"
    assert not any(gpg._subagent_refusal(s) for s in segs), [s.argv for s in segs]


#: Writes that the API-merge refusal (#2768) claims first, for every session: a
#: REST merge, or a GraphQL query it cannot read. Its message, not this rule's,
#: is the one a subagent sees. Pinned so the set cannot quietly grow: a write
#: outside it must still carry the subagent message.
_API_MERGE_REFUSED = frozenset(
    {
        "gh -X PUT api repos/o/r/pulls/5/merge",
        "gh api graphql -F query=@q.graphql",
        "gh api graphql --input q.json",
        'gh api graphql -f query="$(cat q.graphql)"',
        'gh api graphql -f query="$Q"',
        "gh api graphql -f owner=o",
    }
)


@pytest.mark.parametrize("command", _GITHUB_WRITES)
def test_a_subagent_github_write_is_denied(monkeypatch, tmp_path, capsys, command) -> None:
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, _SUBAGENT)
    # The decision: refused (exit 2) with no prompt and no note, whichever rule
    # refuses it. Never allowed, never asked.
    assert rc == 2, (command, rc, out, err)
    assert not out.strip(), out  # no prompt, no note: the owner sees nothing
    assert err.startswith("BLOCKED:"), (command, err)  # a refusal, not a crash
    segs, _ = gpg.analyze_checked(command)
    claimed = gpg._api_merge_deny(
        segs, command, {"tool_input": {"command": command}, "cwd": str(repo)}
    ) is not None
    assert claimed == (command in _API_MERGE_REFUSED), command
    if not claimed:
        assert "only the main session publishes" in err, (command, err)
        assert "git rev-parse HEAD" in err, err


@pytest.mark.parametrize("command", _NOT_ON_THE_READ_LIST)
def test_a_subagent_command_off_the_read_list_is_denied(
    monkeypatch, tmp_path, capsys, command
) -> None:
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, _SUBAGENT)
    assert rc == 2, (command, rc, out, err)
    assert not out.strip(), out
    assert "only the main session publishes" in err, (command, err)


@pytest.mark.parametrize("command", _PROGRAM_IS_AN_EXPANSION)
def test_a_subagent_command_whose_program_is_an_expansion_is_refused(
    monkeypatch, tmp_path, capsys, command
) -> None:
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, _SUBAGENT)
    assert rc == 2 and "cannot be checked against" in err, (command, rc, out, err)
    assert not out.strip(), out


#: The merge arm refuses a merge help request that carries a PR number or flags
#: before this rule runs, in every session (only the bare form skips it).
@pytest.mark.parametrize("command", [c for c in _GITHUB_READS if not c.startswith("gh pr merge")])
def test_a_subagent_read_runs_through_the_hook(monkeypatch, tmp_path, capsys, command) -> None:
    """The whole hook, not only the classifier: a listed read is never refused."""
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, _SUBAGENT)
    assert "only the main session publishes" not in err, (command, err)
    assert "cannot be checked against" not in err, (command, err)
    assert rc == 0, (command, rc, out, err)


@pytest.mark.parametrize(
    "command",
    [
        "gh auth login --git-protocol ssh",
        "git lfs lock big.bin",
        "gh co 5",
        "git co main",
        "gh pr frobnicate 5",
        "git subtree split --prefix docs",
        "gh pr view 5 \\\n  --json title",
    ],
)
def test_the_main_session_is_unchanged_by_the_read_list(
    monkeypatch, tmp_path, capsys, command
) -> None:
    """Main-session control for the allowlist: none of these is refused or asked
    by the subagent rule (none is one of the guard's other gated operations)."""
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo)
    assert "only the main session publishes" not in err, (command, err)
    assert "cannot be checked against" not in err, (command, err)
    assert rc == 0 and "permissionDecision" not in out, (command, rc, out, err)


def test_a_bounded_subagent_command_naming_git_is_refused(monkeypatch, tmp_path, capsys) -> None:
    """CodeRabbit Major: a command over the length bound returns no segments, and
    the shell can build `push` at run time, so naming `git` is enough to refuse."""
    command = "true " + "x" * 70000 + "; git \"$(printf '\\x70\\x75\\x73\\x68')\""
    segs, blind = gpg.analyze_checked(command)
    assert segs == [] and blind is not None and blind.bounds_induced, (segs, blind)
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, _SUBAGENT)
    assert rc == 2 and "one line" in err, (rc, out, err[:300])
    assert not out.strip(), out


@pytest.mark.parametrize("command", _GITHUB_WRITES)
def test_the_classifier_calls_every_listed_write_a_write(command) -> None:
    """The same list at the classifier, so a miss names the segment rather than
    depending on whatever else `main()` would have done with it."""
    segs, _ = gpg.analyze_checked(command)
    assert any(gpg._subagent_refusal(s) for s in segs), command


@pytest.mark.parametrize("command", _GITHUB_READS)
def test_a_subagent_github_read_is_not_refused(command) -> None:
    segs, _ = gpg.analyze_checked(command)
    assert not any(gpg._subagent_refusal(s) for s in segs), command


@pytest.mark.parametrize(
    "command",
    [
        "gh pr comment 5 --body x",
        "gh pr review 5 --approve",
        "gh issue comment 7 --body x",
        "gh api -X POST repos/o/r/issues/5/comments -f body=x",
        "gh api graphql -f query='mutation { x }'",
        "gh release create v1",
    ],
)
def test_a_main_thread_github_write_is_not_refused_by_this_rule(
    monkeypatch, tmp_path, capsys, command
) -> None:
    """Main-session control: the same writes from the main thread are untouched
    (none of these is one the guard otherwise gates)."""
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo)
    assert "only the main session publishes" not in err, (command, err)
    assert rc == 0, (command, rc, out, err)


def test_a_subagent_review_request_is_denied_before_the_round_lookup(
    monkeypatch, tmp_path, capsys
) -> None:
    """The prompt that reached the owner: a review request the round budget turns
    into an ask. From the main thread it still asks; from a subagent it is refused
    and the budget lookup (a network call) never runs."""
    calls = []

    def fake_escalation(segs, cmd="", payload=None):
        calls.append(cmd)
        return "ask", "review round needs approval"

    monkeypatch.setattr(gpg, "_check_codex_round_escalation", fake_escalation)
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    command = "gh pr comment 5 --body '@codex review'"
    rc, out, err = _run_at(monkeypatch, capsys, command, repo)
    _assert_asks(rc, out, err)
    assert len(calls) == 1
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, _SUBAGENT)
    assert rc == 2 and "only the main session publishes" in err, (rc, out, err)
    assert len(calls) == 1, "the subagent's request reached the round lookup"
