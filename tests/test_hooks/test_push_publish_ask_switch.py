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
    for var in gpg._TRANSPORT_ENV:
        monkeypatch.delenv(var, raising=False)


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
