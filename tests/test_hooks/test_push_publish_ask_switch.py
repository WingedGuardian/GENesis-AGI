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


# ─── (a) off + every destination public → no decision, a note ────────────────


@pytest.mark.parametrize(
    "command", ["git push -u origin HEAD", "git push origin feat/x", "git push -u origin @"]
)
def test_a_first_push_to_the_public_repo_is_silenced(
    monkeypatch, tmp_path, capsys, off, command
) -> None:
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, command))


@pytest.mark.parametrize("url", [PUBLIC + ".git", PUBLIC + "/", "https://github.com/Owner/Repo"])
def test_every_https_spelling_of_the_public_remote_qualifies(
    monkeypatch, tmp_path, capsys, off, url
) -> None:
    rc, out, err = _run(
        monkeypatch, tmp_path, capsys, "git push -u origin HEAD", (("remote.origin.url", url),)
    )
    _assert_silenced(rc, out, err)


@pytest.mark.parametrize(
    "url", ["git@github.com:owner/repo.git", "ssh://git@github.com/owner/repo"]
)
def test_an_ssh_form_origin_always_asks(monkeypatch, tmp_path, capsys, off, url) -> None:
    """The ssh transport is a program; only the https form qualifies."""
    rc, out, err = _run(
        monkeypatch, tmp_path, capsys, "git push -u origin HEAD", (("remote.origin.url", url),)
    )
    _assert_asks(rc, out, err)


def test_a_rewrite_that_resolves_to_the_public_repo_is_accepted(
    monkeypatch, tmp_path, capsys, off
) -> None:
    """``git remote get-url --push`` applies insteadOf; the RESULT is what is judged."""
    cfg = (("remote.origin.url", "gh:owner/repo"), ("url.https://github.com/.insteadOf", "gh:"))
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD", cfg))


def test_a_raw_public_url_with_no_rewrite_rule_is_silenced(
    monkeypatch, tmp_path, capsys, off
) -> None:
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, f"git push {PUBLIC} HEAD"))


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
    rc, out, err = _run(monkeypatch, tmp_path, capsys, command, git_config)
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
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD")
    _assert_asks(rc, out, err)
    assert "push_publish" not in out


def test_the_default_still_asks_for_a_publishing_create(
    monkeypatch, tmp_path, capsys, create_publishes
) -> None:
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "gh pr create --title t"))


def test_push_publish_on_still_asks(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=on")
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD"))


def test_a_misconfigured_key_asks_and_says_so(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("_TEST_HOOK_ASK_POLICY", "push_publish=maybe")
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD")
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
        "git push -u origin HEAD && gh pr create --title t",
        "git config remote.origin.url https://github.com/owner/other && git push -u origin HEAD",
        "git remote set-url origin https://github.com/owner/other && git push -u origin HEAD",
    ],
)
def test_a_compound_with_another_operation_still_asks(
    monkeypatch, tmp_path, capsys, off, command
) -> None:
    rc, out, err = _run(monkeypatch, tmp_path, capsys, command)
    assert rc == 0, (command, rc, out, err)
    assert _decision(out) == "ask", (command, out)


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
    _assert_silenced(*_run_with_tracking(monkeypatch, tmp_path, capsys, "git push", cfg, False))


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
        monkeypatch, tmp_path, capsys, command, (("remote.origin.url", PUBLIC), *extra)
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
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD"))


def test_a_proxy_plus_no_verify_environment_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:1")
    monkeypatch.setenv("GIT_SSL_NO_VERIFY", "1")
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD"))


def test_an_unreadable_http_config_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    real = gpg._run_git_lines

    def fake(argv):
        if "--get-regexp" in argv and r"^http\." in argv:
            return None
        return real(argv)

    monkeypatch.setattr(gpg, "_run_git_lines", fake)
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD"))


def test_a_configured_url_with_trailing_whitespace_still_asks(
    monkeypatch, tmp_path, capsys, off
) -> None:
    rc, out, err = _run(
        monkeypatch,
        tmp_path,
        capsys,
        "git push -u origin HEAD",
        (("remote.origin.url", PUBLIC + " "),),
    )
    _assert_asks(rc, out, err)


def test_a_raw_url_with_trailing_whitespace_still_asks(monkeypatch, tmp_path, capsys, off) -> None:
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, f"git push -u '{PUBLIC} ' HEAD"))


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
    _assert_asks(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD"))


def test_the_probe_is_asked_about_the_resolved_url_and_branch(
    monkeypatch, tmp_path, capsys, off
) -> None:
    seen = []
    monkeypatch.setattr(
        gpg, "_remote_branch_definitely_absent", lambda url, branch, cwd: seen.append((url, branch)) or True
    )
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD"))
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
    rc, out, err = _run(monkeypatch, tmp_path, capsys, "git push -u origin HEAD")
    _assert_silenced(rc, out, err)
    ctx = _hso(out)["additionalContext"]
    assert "NOTE:" in ctx and "force_push" in ctx, ctx


# ─── round 2 (terminal): only a single plain `git push` is ever silenced ─────


def _hook_file(repo: Path, name: str, body: str) -> None:
    hook = repo / ".git" / "hooks" / name
    hook.write_text("#!/bin/sh\n" + body + "\n")
    hook.chmod(0o755)


@pytest.mark.parametrize(
    "command",
    [
        "cd sub && git push -u origin HEAD",
        "cd sub | git push -u origin HEAD",
        "cd sub & git push -u origin HEAD",
        "cd sub\ngit push -u origin HEAD",
        "cd sub; git push -u origin HEAD",
        "git push -u origin HEAD & git rev-parse HEAD",
        "git push -u origin HEAD > out.txt",
        "(git push -u origin HEAD)",
        "git push -u origin HEAD && true",
        "git -C . push -u origin HEAD",
        "git -P push -u origin HEAD",
        "git --no-pager push -u origin HEAD",
        "env git push -u origin HEAD",
        "command git push -u origin HEAD",
        "X=1 git push -u origin HEAD",
        "\\git push -u origin HEAD",
    ],
)
def test_anything_but_a_single_plain_push_asks(monkeypatch, tmp_path, capsys, off, command) -> None:
    rc, out, err = _run(monkeypatch, tmp_path, capsys, command)
    assert rc == 0, (command, rc, out, err)
    assert _decision(out) == "ask", (command, out, err)


def test_a_cdpath_cd_before_the_push_asks(monkeypatch, tmp_path, capsys, off) -> None:
    """Codex P1: with CDPATH set, `cd sub` can land in an unrelated repo while the
    guard resolves `sub` against the payload cwd."""
    other = _repo(tmp_path / "cdpath" / "sub", (("remote.origin.url", OTHER),))
    monkeypatch.setenv("CDPATH", str(other.parent.parent))
    rc, out, err = _run(monkeypatch, tmp_path / "main", capsys, "cd sub && git push -u origin HEAD")
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
def test_a_neighbour_that_runs_code_before_the_push_asks(
    monkeypatch, tmp_path, capsys, off, command, setup
) -> None:
    """Class audit: each "inert" neighbour can run configured code that changes
    the push after the hook judged it."""
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
    _assert_asks(rc, out.out, out.err)


def test_a_dash_C_through_a_symlink_asks(monkeypatch, tmp_path, capsys, off) -> None:
    """git follows `lnk` before applying `..`; os.path.normpath does not."""
    import os

    other = _repo(tmp_path / "other", (("remote.origin.url", OTHER),))
    (other / "sub").mkdir()
    repo = _repo(tmp_path / "main")
    os.symlink(other / "sub", repo / "lnk")
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "git -C lnk/.. push -u origin HEAD"},
        "cwd": str(repo),
    }
    monkeypatch.setattr(gpg.sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.chdir(repo)
    rc = gpg.main()
    out = capsys.readouterr()
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
    _assert_silenced(*_run(monkeypatch, tmp_path, capsys, command))


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
    rc, out, err = _run(monkeypatch, tmp_path, capsys, command)
    if rc == 2:
        return  # refused outright by another gate: not silenced, which is the point
    assert _decision(out) == "ask", (command, out, err)


@pytest.mark.parametrize("suffix", ["", " 2>&1 | tail -2", " | tail -n 5"])
def test_dash_C_to_a_sibling_worktree_is_silenced(monkeypatch, tmp_path, capsys, off, suffix):
    """The measured incident shape: the session sits in the main checkout and
    publishes a linked worktree's branch with `-C`."""
    repo, wt = _with_worktree(tmp_path)
    command = f"git -C {wt} push -u origin HEAD{suffix}"
    rc, out, err = _run_at(monkeypatch, capsys, command, repo)
    _assert_silenced(rc, out, err)
    assert "feat/wt" in _hso(out)["additionalContext"], out  # judged in the worktree


def test_dash_C_to_the_sessions_own_checkout_is_silenced(monkeypatch, tmp_path, capsys, off):
    repo, _wt = _with_worktree(tmp_path)
    _assert_silenced(*_run_at(monkeypatch, capsys, f"git -C {repo} push -u origin HEAD", repo))


def test_dash_C_from_a_worktree_back_to_the_main_checkout_is_silenced(
    monkeypatch, tmp_path, capsys, off
):
    repo, wt = _with_worktree(tmp_path)
    _assert_silenced(*_run_at(monkeypatch, capsys, f"git -C {repo} push -u origin HEAD", wt))


def test_dash_C_through_a_symlink_to_a_sibling_worktree_asks(monkeypatch, tmp_path, capsys, off):
    import os

    repo, wt = _with_worktree(tmp_path)
    link = _real(tmp_path) / "lnk"
    os.symlink(wt, link)
    _assert_asks(*_run_at(monkeypatch, capsys, f"git -C {link} push -u origin HEAD", repo))


def test_dash_C_through_a_symlinked_parent_asks(monkeypatch, tmp_path, capsys, off):
    import os

    repo, wt = _with_worktree(tmp_path)
    alias = _real(tmp_path) / "alias"
    os.symlink(wt.parent, alias)
    command = f"git -C {alias / wt.name} push -u origin HEAD"
    _assert_asks(*_run_at(monkeypatch, capsys, command, repo))


def test_dash_C_to_another_repository_asks(monkeypatch, tmp_path, capsys, off):
    """Same public origin, but a DIFFERENT repository: its hooks and config are
    not the ones this session's checkout runs under."""
    repo, _wt = _with_worktree(tmp_path)
    other = _repo(_real(tmp_path) / "elsewhere", (("remote.origin.url", PUBLIC),))
    _assert_asks(*_run_at(monkeypatch, capsys, f"git -C {other} push -u origin HEAD", repo))


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
    command = f"git -C {spelling.format(wt=wt)} push -u origin HEAD"
    rc, out, err = _run_at(monkeypatch, capsys, command, repo)
    if rc == 2:
        return
    assert _decision(out) == "ask", (command, out, err)


def test_dash_C_without_a_known_session_cwd_asks(monkeypatch, tmp_path, capsys, off):
    """No payload cwd: there is nothing to compare the repository against."""
    repo, wt = _with_worktree(tmp_path)
    monkeypatch.chdir(repo)
    _assert_asks(*_run_at(monkeypatch, capsys, f"git -C {wt} push -u origin HEAD", None))


def test_dash_C_from_a_session_outside_any_repository_asks(monkeypatch, tmp_path, capsys, off):
    _repo_dir, wt = _with_worktree(tmp_path)
    elsewhere = _real(tmp_path) / "plain"
    elsewhere.mkdir()
    _assert_asks(*_run_at(monkeypatch, capsys, f"git -C {wt} push -u origin HEAD", elsewhere))


@pytest.mark.parametrize("var", _REPO_ENV)
def test_a_repository_selecting_variable_in_the_environment_asks(
    monkeypatch, tmp_path, capsys, off, var
) -> None:
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    # Set only for the guard's own decision, after the fixture repo exists: a
    # repository variable in the environment would otherwise redirect the setup.
    monkeypatch.setenv(var, str(repo / ".git") if var != "GIT_NAMESPACE" else "ns")
    _assert_asks(*_run_at(monkeypatch, capsys, "git push -u origin HEAD", repo))


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
    assert "subagents do not publish" in err, err
    assert "git rev-parse HEAD" in err and "main session" in err, err
    assert not out.strip(), out  # no prompt, no note: the owner sees nothing


def test_a_subagent_repush_of_a_published_branch_is_denied(monkeypatch, tmp_path, capsys) -> None:
    """The re-push relaxation would ALLOW this from the main thread; a subagent
    still does not publish."""
    monkeypatch.setattr(gpg, "_push_is_republish", lambda *a, **k: True)
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, "git push origin feat/x", repo, _SUBAGENT)
    assert rc == 2 and "subagents do not publish" in err, (rc, out, err)


def test_a_dispatched_subagent_is_still_denied(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(gpg, "_is_dispatched", lambda: True)
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, "git push -u origin HEAD", repo, _SUBAGENT)
    assert rc == 2 and "BLOCKED" in err, (rc, out, err)


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
    rc, out, err = _run_at(monkeypatch, capsys, "git push -u origin HEAD", repo, extra)
    _assert_silenced(rc, out, err)
    assert "subagents do not publish" not in err


def test_a_main_thread_push_still_asks_by_default(monkeypatch, tmp_path, capsys) -> None:
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, "git push -u origin HEAD", repo, {"agent_type": "x"})
    _assert_asks(rc, out, err)


@pytest.mark.parametrize(
    "command", ["git status", "git commit --allow-empty -m x", "gh pr view 5", "git log -1"]
)
def test_a_subagent_that_does_not_publish_is_not_refused(monkeypatch, tmp_path, capsys, command):
    repo = _repo(tmp_path, (("remote.origin.url", PUBLIC),))
    rc, out, err = _run_at(monkeypatch, capsys, command, repo, _SUBAGENT)
    assert "subagents do not publish" not in err, (command, err)
    assert rc == 0, (command, rc, out, err)
