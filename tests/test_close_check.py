"""close_check: the read-only boundary a closing verification runs a subagent's check through.

Every refusal and pin here answers a bypass that a review reproduced on git 2.43
(2026-10-28). The git cases run against a scratch repo with an ignored
``secrets.env``, so "the secret did not print" and "the program did not run"
are observed, not argued.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from tests.conftest import private_module

cc = private_module(
    "close_check", Path(__file__).resolve().parents[1] / "scripts" / "close_check.py"
)

REPO = "owner/repo"
SECRET = "SEKRIT-canary-7c1f"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@example.com", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout  # fmt: skip


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "a.txt").write_text("alpha\n")
    (r / ".gitignore").write_text("secrets.env\n")
    (r / "secrets.env").write_text(f"TOKEN={SECRET}\n")
    _git(r, "add", "a.txt", ".gitignore")
    _git(r, "commit", "-qm", "init")
    return r


def _run(check, repo: Path, tmp_path: Path, **kw):
    return cc.run(check, repo_root=repo, db_path=tmp_path / "none.db", repo=REPO, **kw)


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").strip()


# -- git templates ---------------------------------------------------------------


def test_each_git_template_runs_and_records(repo, tmp_path):
    head = _head(repo)
    cases = [
        (cc.GitCheck("rev-parse", "main"), head),
        (cc.GitCheck("log", head, format="%H %s"), f"{head} init"),
        (cc.GitCheck("log", "main", path="a.txt", format="%h"), head[:7]),
        (cc.GitCheck("show-file", head, path="a.txt"), "alpha"),
        (cc.GitCheck("ls-tree", "main"), "a.txt"),
        (cc.GitCheck("contains", head, ref="refs/heads/main"), "refs/heads/main"),
    ]
    for check, expected in cases:
        res = _run(check, repo, tmp_path)
        assert res.ok, (check, res)
        assert cc.matches(res, expected), (check, res.stdout_head)
        assert res.stdout_sha256 == hashlib.sha256(res.stdout_head.encode()).hexdigest()


@pytest.mark.parametrize(
    "check",
    [
        cc.GitCheck("diff", "HEAD"),
        cc.GitCheck("blame", "HEAD", path="a.txt"),
        cc.GitCheck("rev-parse", "-Ssecrets.env"),
        cc.GitCheck("rev-parse", "--output=x"),
        cc.GitCheck("rev-parse", "HEAD..main"),
        cc.GitCheck("rev-parse", "HEAD:secrets.env"),
        cc.GitCheck("log", "HEAD", format="%G?"),
        cc.GitCheck("log", "HEAD", format="%GG"),
        cc.GitCheck("log", "HEAD", format="%(trailers)"),
        cc.GitCheck("log", "HEAD", format="%Cred%H"),
        cc.GitCheck("log", "HEAD", count=51),
        cc.GitCheck("log", "HEAD", path="secrets.env"),
        cc.GitCheck("show-file", "HEAD", path="secrets.env"),
        cc.GitCheck("show-file", "HEAD", path="../outside.txt"),
        cc.GitCheck("show-file", "HEAD", path="/etc/passwd"),
        cc.GitCheck("show-file", "HEAD", path=":(top)secrets.env"),
        cc.GitCheck("show-file", "HEAD", path="-Ssecrets.env"),
        cc.GitCheck("show-file", "HEAD"),
        cc.GitCheck("contains", "HEAD", ref="main"),
        cc.GitCheck("contains", "HEAD", ref="refs/heads/../x"),
    ],
)
def test_git_refusals_run_nothing(check, repo, tmp_path):
    res = _run(check, repo, tmp_path)
    assert res.refused, check
    assert res.exit_code is None and not res.stdout_head


def _marker_script(tmp_path: Path, name: str) -> tuple[Path, Path]:
    marker = tmp_path / f"{name}.ran"
    script = tmp_path / f"{name}.sh"
    script.write_text(f"#!/bin/sh\necho ran >> {marker}\ncat\n")
    script.chmod(0o755)
    return script, marker


def test_repo_config_programs_do_not_run(repo, tmp_path):
    """fsmonitor, a process/clean/smudge filter named from .git/info/attributes
    and from core.attributesFile, and a textconv driver: none runs on any template."""
    fsm, fsm_ran = _marker_script(tmp_path, "fsm")
    flt, flt_ran = _marker_script(tmp_path, "filter")
    attrs = tmp_path / "attrs"
    attrs.write_text("*.txt filter=pwn diff=pwn\n")
    (repo / ".git" / "info").mkdir(exist_ok=True)
    (repo / ".git" / "info" / "attributes").write_text("*.txt filter=pwn diff=pwn\n")
    for key, value in (
        ("core.fsmonitor", str(fsm)),
        ("filter.pwn.clean", str(flt)),
        ("filter.pwn.smudge", str(flt)),
        ("diff.pwn.textconv", str(flt)),
        ("core.attributesFile", str(attrs)),
    ):
        _git(repo, "config", key, value)
    (repo / "a.txt").write_text("changed\n")
    head = _head(repo)
    for check in (
        cc.GitCheck("log", head, path="a.txt"),
        cc.GitCheck("show-file", head, path="a.txt"),
        cc.GitCheck("ls-tree", head, path="a.txt"),
        cc.GrepCheck(pattern="alpha|changed", path="a.txt"),
    ):
        assert not _run(check, repo, tmp_path).refused, check
    assert not fsm_ran.exists() and not flt_ran.exists()


def test_a_signature_program_does_not_run(repo, tmp_path):
    """gpg.ssh.program and gpg.x509.program are repo-config programs that git
    runs to verify a signed commit; neither runs, whatever the format asks."""
    prog, ran = _marker_script(tmp_path, "gpg")
    allowed = tmp_path / "allowed_signers"
    allowed.write_text("")
    _git(repo, "config", "gpg.ssh.program", str(prog))
    _git(repo, "config", "gpg.x509.program", str(prog))
    _git(repo, "config", "gpg.ssh.allowedSignersFile", str(allowed))
    _git(repo, "config", "log.showSignature", "true")
    tree = _git(repo, "rev-parse", "HEAD^{tree}").strip()
    body = (
        f"tree {tree}\nauthor t <t@example.com> 0 +0000\ncommitter t <t@example.com> 0 +0000\n"
        "gpgsig -----BEGIN SSH SIGNATURE-----\n U1NIU0lH\n -----END SSH SIGNATURE-----\n\nsigned\n"
    )
    signed = subprocess.run(
        ["git", "hash-object", "-t", "commit", "-w", "--stdin"], cwd=repo, input=body,
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    res = _run(cc.GitCheck("log", signed, format="%H %s"), repo, tmp_path)
    assert res.ok and signed in res.stdout_head
    assert not ran.exists()


def test_a_read_never_writes_the_real_index(repo, tmp_path):
    index = repo / ".git" / "index"
    before = index.stat().st_mtime_ns
    time.sleep(0.01)
    os.utime(repo / "a.txt")  # stale stat data: git refreshes the index on a worktree read
    assert _run(cc.GrepCheck(pattern="alpha", path="a.txt"), repo, tmp_path).ok
    assert index.stat().st_mtime_ns == before


# -- grep -----------------------------------------------------------------------------


def test_the_secret_never_prints_through_git_grep(repo, tmp_path):
    res = _run(cc.GrepCheck(pattern="TOKEN", path="."), repo, tmp_path)
    assert SECRET not in res.stdout_head
    assert _run(cc.GrepCheck(pattern="TOKEN", path="secrets.env"), repo, tmp_path).refused


def test_a_path_through_a_symlink_is_refused(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "b.txt").write_text("beta\n")
    (repo / "sub").symlink_to(outside)
    assert _run(cc.GrepCheck(pattern="beta", path="sub/b.txt"), repo, tmp_path).refused


def test_log_grep_reads_only_a_file_under_the_logs_dir(tmp_path, monkeypatch, repo):
    home = tmp_path / "gh"
    (home / "logs").mkdir(parents=True)
    (home / "logs" / "server.log").write_text("ready\n")
    (tmp_path / "elsewhere.log").write_text("ready\n")
    (home / "logs" / "escape.log").symlink_to(tmp_path / "elsewhere.log")
    monkeypatch.setenv("GENESIS_HOME", str(home))
    ok = _run(cc.GrepCheck("ready", str(home / "logs" / "server.log")), repo, tmp_path)
    assert ok.ok and "ready" in ok.stdout_head
    for path in (
        home / "logs",
        home / "logs" / "escape.log",
        tmp_path / "elsewhere.log",
        home / "logs" / "../x",
    ):
        assert _run(cc.GrepCheck("ready", str(path)), repo, tmp_path).refused, path


# -- gh templates --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("check", "argv"),
    [
        (cc.GhCheck("pr-view", 12, fields=("state", "mergeCommit")),
         ["gh", "pr", "view", "12", "-R", REPO, "--json", "state,mergeCommit"]),
        (cc.GhCheck("pr-checks", 12), ["gh", "pr", "checks", "12", "-R", REPO]),
        (cc.GhCheck("run-list", sha="abc1234"),
         ["gh", "run", "list", "-R", REPO, "--commit", "abc1234", "--limit", "50",
          "--json", "name,workflowName,status,conclusion,headSha"]),
        (cc.GhCheck("api", path="pulls/12"), ["gh", "api", f"repos/{REPO}/pulls/12"]),
    ],
)  # fmt: skip
def test_gh_templates_build_fixed_argv(check, argv):
    assert cc.plan_gh(check, REPO) == argv


@pytest.mark.parametrize(
    "check",
    [
        cc.GhCheck("pr-comment", 12),
        cc.GhCheck("pr-view", 12),
        cc.GhCheck("pr-view", 12, fields=("body",)),
        cc.GhCheck("pr-view", 12, fields=("state", "-R")),
        cc.GhCheck("pr-view", None, fields=("state",)),
        cc.GhCheck("pr-view", True, fields=("state",)),
        cc.GhCheck("pr-checks", -1),
        cc.GhCheck("run-list", sha="main"),
        cc.GhCheck("api", path="../../user"),
        cc.GhCheck("api", path="pulls/../../../user"),
        cc.GhCheck("api", path="issues?state=all"),
        cc.GhCheck("api", path="https://example.com/x"),
        cc.GhCheck("api", path=""),
        cc.GhCheck("api", path="a//b"),
    ],
)
def test_gh_refusals(check):
    with pytest.raises(cc.Refused):
        cc.plan_gh(check, REPO)


def test_gh_refuses_a_malformed_repo():
    with pytest.raises(cc.Refused):
        cc.plan_gh(cc.GhCheck("pr-checks", 1), "owner/repo --web")


# -- journal ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("unit", "since", "pattern"),
    [
        ("genesis-server --merge", "-1h", "x"),
        ("genesis-server", "-1h --file=/x", "x"),
        ("genesis-server", "yesterday", "x"),
        ("genesis-server", "-1h", ""),
        ("genesis-server", "-1h", "x" * 513),
    ],
)
def test_journal_refusals(unit, since, pattern):
    with pytest.raises(cc.Refused):
        cc.plan_journal(cc.JournalCheck(unit, since, pattern))


def test_a_journal_pattern_cannot_become_a_flag():
    argv = cc.plan_journal(cc.JournalCheck("genesis-server", "-2h", "-f"))
    assert argv[-1] == "--grep=-f" and "-f" not in argv


# -- sql ----------------------------------------------------------------------------------


def test_sql_reads_in_process_and_refuses_a_write(tmp_path, repo):
    db = tmp_path / "t.db"
    with sqlite3.connect(db) as con:
        con.execute("CREATE TABLE t (v TEXT)")
        con.execute("INSERT INTO t VALUES ('merged')")
    res = cc.run(cc.SqlCheck("SELECT v FROM t"), repo_root=repo, db_path=db, repo=REPO)
    assert res.ok and '"merged"' in res.stdout_head
    bad = cc.run(cc.SqlCheck("DELETE FROM t"), repo_root=repo, db_path=db, repo=REPO)
    assert bad.exit_code == 1 and not bad.ok
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT count(*) FROM t").fetchone() == (1,)


# -- runner ----------------------------------------------------------------------------------


def test_a_preparation_failure_is_a_refusal_not_a_raise(repo, tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk gone")

    monkeypatch.setattr(cc, "_index_copy", boom)
    res = _run(cc.GitCheck("rev-parse", "main"), repo, tmp_path)
    assert res.refused and "OSError" in res.refused


def test_a_timeout_kills_the_whole_group(tmp_path):
    pidfile = tmp_path / "pid"
    argv = ["/bin/sh", "-c", f"sleep 60 & echo $! > {pidfile}; sleep 60"]
    started = time.monotonic()
    code, timed_out, _, _ = cc._execute(argv, tmp_path, {"PATH": "/usr/bin:/bin"}, 0.5)
    assert timed_out and code is None and time.monotonic() - started < 15
    grandchild = int(pidfile.read_text())
    time.sleep(0.2)
    assert (
        not Path(f"/proc/{grandchild}").exists()
        or "Z" in Path(f"/proc/{grandchild}/stat").read_text().split()[2]
    )


def test_a_grandchild_holding_the_pipe_does_not_hang_the_drain(tmp_path):
    argv = ["/bin/sh", "-c", "sleep 60 & echo done"]
    started = time.monotonic()
    code, timed_out, out, _ = cc._execute(argv, tmp_path, {"PATH": "/usr/bin:/bin"}, 30)
    assert not timed_out and code == 0 and out["head"] == b"done\n"
    assert time.monotonic() - started < 15


def test_large_output_is_drained_capped_and_hashed(tmp_path):
    argv = ["/bin/sh", "-c", "head -c 200000 /dev/zero"]
    code, _, out, _ = cc._execute(argv, tmp_path, {"PATH": "/usr/bin:/bin"}, 30)
    assert code == 0 and out["total"] == 200000 and len(out["head"]) == cc.STDOUT_CAP
    assert out["sha"] == hashlib.sha256(b"\0" * 200000).hexdigest()


def test_the_environment_is_built_from_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("GH_TOKEN", "should-not-pass")
    monkeypatch.setenv("SECRETS_PATH", "/x")
    monkeypatch.setenv("GIT_DIR", "/x")
    for kind in ("git", "gh", "journal", "grep"):
        env = cc._env(str(tmp_path), kind, None)
        assert not {"GH_TOKEN", "SECRETS_PATH", "GIT_DIR"} & set(env)
        assert env["PATH"] == "/usr/bin:/bin" and env["HOME"] == str(tmp_path)
    assert "GH_CONFIG_DIR" in cc._env(str(tmp_path), "gh", None)
    assert "GH_CONFIG_DIR" not in cc._env(str(tmp_path), "git", None)


def test_a_catastrophic_expect_is_no_match_not_a_hang(monkeypatch):
    monkeypatch.setattr(cc, "EXPECT_TIMEOUT_S", 0.2)
    res = cc.CheckRun("git", ("git",), exit_code=0, stdout_head="a" * 5000 + "!")
    started = time.monotonic()
    assert cc.matches(res, r"(a+)+$") is False
    assert time.monotonic() - started < 5


# -- second security review: malformed fields, api heads, gh state ----------------------


@pytest.mark.parametrize(
    "check",
    [
        cc.GrepCheck("x", None),
        cc.GrepCheck("\ud800", "."),
        cc.JournalCheck(None, "-1h", "x"),
        cc.JournalCheck("genesis-server", None, "x"),
        cc.JournalCheck("genesis-server", "-1h", "\ud800"),
        cc.GhCheck("pr-view", 1, fields=None),
        cc.GhCheck("pr-view", 1, fields=(["a"],)),
        cc.GitCheck("log", None),
        cc.GitCheck("log", "HEAD", format=None),
        cc.GitCheck("log", "HEAD", count="5"),
    ],
)
def test_a_malformed_field_is_a_refusal_not_a_raise(check, repo, tmp_path):
    res = _run(check, repo, tmp_path)
    assert res.refused and res.exit_code is None


@pytest.mark.parametrize(
    "path",
    ["hooks", "hooks/1/config", "keys", "collaborators", "invitations", "environments",
     "actions/variables", "actions/secrets", "actions", "dependabot/alerts"],
)  # fmt: skip
def test_api_refuses_credential_bearing_resources(path):
    with pytest.raises(cc.Refused):
        cc.plan_gh(cc.GhCheck("api", path=path), REPO)


@pytest.mark.parametrize("path", ["commits/abc1234", "actions/runs/12", "check-runs/9", "git/ref/heads/main"])
def test_api_reads_build_and_merge_state(path):
    assert cc.plan_gh(cc.GhCheck("api", path=path), REPO)[-1] == f"repos/{REPO}/{path}"


@pytest.mark.parametrize("repo_arg", ["o/..", "o/.", "-o/r", "o/.hidden"])
def test_a_dot_or_dash_repo_name_is_refused(repo_arg):
    with pytest.raises(cc.Refused):
        cc.plan_gh(cc.GhCheck("pr-checks", 1), repo_arg)


def test_non_ascii_digits_are_not_a_since():
    with pytest.raises(cc.Refused):
        cc.plan_journal(cc.JournalCheck("genesis-server", "-١h", "x"))


def test_an_expect_that_does_not_compile_is_no_match():
    res = cc.CheckRun("git", ("git",), exit_code=0, stdout_head="abc")
    assert cc.matches(res, "(") is False


def test_gh_runs_on_a_copy_of_its_config(tmp_path, monkeypatch):
    real = tmp_path / "real-gh"
    real.mkdir()
    (real / "hosts.yml").write_text("github.com: {}\n")
    monkeypatch.setenv("GH_CONFIG_DIR", str(real))
    home = tmp_path / "home"
    home.mkdir()
    cc._gh_config_copy(str(home))
    env = cc._env(str(home), "gh", None)
    assert env["GH_CONFIG_DIR"] == str(home / "gh")
    assert (home / "gh" / "hosts.yml").read_text() == "github.com: {}\n"
