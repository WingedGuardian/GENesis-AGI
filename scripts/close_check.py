#!/usr/bin/env python3
"""Run ONE read-only check that re-derives a claim when a verification row closes.

A validating session's ``scripts/pr_verification.py close`` runs a check that a
subagent wrote into its evidence document. The check is subagent-authored and
close runs it outside every PreToolUse guard, so this module is the boundary:
it must only read. Every check is a named template whose argv THIS module
builds from validated fields; no caller-supplied flag or argv reaches a program.

- ``SqlCheck``: one statement through ``genesis.db.readonly_query``, in process.
- ``GitCheck``: ``rev-parse``, ``log`` (allowlisted ``--format`` atoms),
  ``show-file`` (a blob at ``rev:path``), ``ls-tree`` and ``contains`` (which
  refs contain a commit).
- ``GhCheck``: ``pr-view`` (allowlisted ``--json`` fields), ``pr-checks``,
  ``run-list`` (runs for one commit) and ``api`` (a GET under ``repos/<repo>/``).
- ``JournalCheck`` and ``GrepCheck``: a unit's recent journal, and one pattern
  over a repo path (``git grep``) or one file under ``~/.genesis/logs``.

Why templates and not a free git/gh argv (ruling 2026-10-28): two security
reviews of a free argv checked against a refusal list each found new ways to
read an ignored file or run a program. ``blame -S<file>`` and
``--ignore-revs-file=<file>`` print any file into an error message, and ``gh -cR``
hides a repo flag in a cluster. git's option surface is open, so a refusal list
over it never closes.

git still runs programs and writes files on its own, so it also runs with
config pins, an empty attribute source and a copy of the index (MEASURED on git
2.43, 2026-10-28). Without them, a repo-config ``core.fsmonitor`` runs on
``git diff``, filter drivers from ``.git/info/attributes`` run on ``diff`` and
``blame``, ``gpg.ssh.program`` runs on ``--show-signature``, and ``diff``
rewrites ``.git/index``.

A refusal runs nothing and never raises: :func:`run` returns a ``CheckRun``
whose ``refused`` says why.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import regex

STDOUT_CAP = 64 * 1024
STDERR_CAP = 4 * 1024
#: The spec's value. It bounds a hung ``gh`` (network) or a slow ``git log``
#: over the whole history; nothing legitimate a claim re-derives takes longer.
TIMEOUT_S = 120.0
EXPECT_TIMEOUT_S = 5.0
SQL_MAX_ROWS = 200
LOG_MAX = 50
_SAFE_PATH = "/usr/bin:/bin"
_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

#: Config that would make a git read run a program, fetch, or page. Each one
#: was either measured running a program or names a program git can run.
_GIT_PINS = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", os.devnull),
    ("core.pager", "cat"),
    ("core.attributesFile", os.devnull),
    ("color.ui", "never"),
    ("diff.external", ""),
    ("gpg.program", "/bin/false"),
    ("gpg.openpgp.program", "/bin/false"),
    ("gpg.x509.program", "/bin/false"),
    ("gpg.ssh.program", "/bin/false"),
    ("log.showSignature", "false"),
    ("protocol.allow", "never"),
)

_REV = regex.compile(r"[A-Za-z0-9][A-Za-z0-9._/~^-]{0,199}")
_SHA = regex.compile(r"[0-9a-f]{7,40}")
_REF = regex.compile(r"refs/(heads|remotes|tags)/[A-Za-z0-9._/-]{1,200}")
_PATH = regex.compile(r"[A-Za-z0-9._][A-Za-z0-9._/ +@=-]{0,255}")
_REPO = regex.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")
#: What ``gh api`` may read under ``repos/<repo>/``: build, merge and release
#: state. Not here, so refused: ``hooks`` (a webhook URL can be the credential),
#: ``keys``, ``collaborators``, ``invitations``, ``environments``, and under
#: ``actions`` everything but runs, jobs and workflows (``actions/variables``
#: holds plaintext values).
_API_HEADS = frozenset(
    [
        "pulls",
        "commits",
        "branches",
        "git",
        "compare",
        "statuses",
        "check-runs",
        "check-suites",
        "issues",
        "releases",
        "tags",
        "contents",
        "actions/runs",
        "actions/jobs",
        "actions/workflows",
    ]
)
_API_SEGMENT = regex.compile(r"[A-Za-z0-9._-]{1,100}")
#: ``git log --format`` atoms that print commit data and nothing else. Not
#: here, so refused: ``%G*`` (runs the signature program), ``%(...)``
#: (trailers, describe), ``%C`` (color), ``%w`` and the rest.
_LOG_ATOMS = regex.compile(r"%(?:H|h|P|p|T|t|s|b|B|an|ae|ad|aI|ai|cn|ce|cd|cI|ci|d|D|n|%)")
_PR_FIELDS = frozenset(
    [
        "number",
        "state",
        "title",
        "url",
        "isDraft",
        "mergedAt",
        "closedAt",
        "mergeCommit",
        "headRefOid",
        "headRefName",
        "baseRefName",
        "labels",
        "reviewDecision",
        "statusCheckRollup",
    ]
)

_UNIT = regex.compile(r"[A-Za-z0-9@._-]{1,128}")
_SINCE = regex.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}( [0-9]{2}:[0-9]{2}(:[0-9]{2})?)?|-[0-9]{1,6}(s|min|h|d)"
)
_PATTERN_MAX = 512


@dataclass(frozen=True)
class SqlCheck:
    sql: str
    params: tuple = ()


@dataclass(frozen=True)
class GitCheck:
    op: str  # rev-parse | log | show-file | ls-tree | contains
    rev: str
    path: str | None = None
    format: str = "%H"
    count: int = 1
    ref: str | None = None


@dataclass(frozen=True)
class GhCheck:
    op: str  # pr-view | pr-checks | run-list | api
    number: int | None = None
    fields: tuple[str, ...] = ()
    sha: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class JournalCheck:
    unit: str
    since: str
    pattern: str


@dataclass(frozen=True)
class GrepCheck:
    pattern: str
    path: str


@dataclass(frozen=True)
class CheckRun:
    kind: str
    command: tuple[str, ...]
    refused: str | None = None
    exit_code: int | None = None
    timed_out: bool = False
    stdout_head: str = ""
    stdout_sha256: str = ""
    stdout_truncated: bool = False
    stderr_head: str = ""

    @property
    def ok(self) -> bool:
        return self.refused is None and not self.timed_out and self.exit_code == 0


class Refused(Exception):
    """The check is not one this module will run; the message says why."""


def logs_dir() -> Path:
    return Path(os.environ.get("GENESIS_HOME") or Path.home() / ".genesis") / "logs"


def _rev(rev: str) -> str:
    if not isinstance(rev, str) or not _REV.fullmatch(rev) or ".." in rev:
        raise Refused(f"rev {rev!r} is not a plain revision (letters, digits, ._/~^-)")
    return rev


def _repo_path(path: str, repo_root: Path, env: dict) -> str:
    """``path`` if it is a plain repo-relative path, reached through no symlink
    and not git-ignored; else Refused."""
    if not isinstance(path, str) or not _PATH.fullmatch(path) or ".." in PurePosixPath(path).parts:
        raise Refused(f"path {path!r} is not a plain path inside the repo")
    cur = repo_root
    for part in PurePosixPath(path).parts:
        cur = cur / part
        if cur.is_symlink():
            raise Refused(f"path {path!r} goes through a symlink")
    ignored = subprocess.run(
        ["/usr/bin/git", "check-ignore", "-q", "--no-index", "--", path],
        cwd=repo_root, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=30,
    )  # fmt: skip
    if ignored.returncode == 0:
        raise Refused(f"path {path!r} is git-ignored")
    return path


def plan_git(check: GitCheck, repo_root: Path, env: dict) -> list[str]:
    rev = _rev(check.rev)
    path = _repo_path(check.path, repo_root, env) if check.path is not None else None
    if check.op == "rev-parse":
        return ["git", "rev-parse", "--verify", "--quiet", "--end-of-options", f"{rev}^{{commit}}"]
    if check.op == "log":
        fmt = check.format
        if (
            not isinstance(fmt, str)
            or not 0 < len(fmt) <= 200
            or "\0" in fmt
            or not fmt.isprintable()
        ):
            raise Refused("log format must be 1 to 200 printable characters")
        if "%" in _LOG_ATOMS.sub("", fmt):
            raise Refused("log format may use only the plain commit atoms (%H %h %s %ad %ci ...)")
        if not isinstance(check.count, int) or not 1 <= check.count <= LOG_MAX:
            raise Refused(f"log count must be 1 to {LOG_MAX}")
        argv = ["git", "log", "-n", str(check.count), "--no-show-signature", "--no-notes",
                f"--format={fmt}", "--end-of-options", rev]  # fmt: skip
        return argv + (["--", path] if path else [])
    if check.op == "show-file":
        if path is None:
            raise Refused("show-file needs a path")
        return ["git", "cat-file", "blob", "--end-of-options", f"{rev}:{path}"]
    if check.op == "ls-tree":
        return ["git", "ls-tree", "--name-only", "--end-of-options", rev] + ([path] if path else [])
    if check.op == "contains":
        if not isinstance(check.ref, str) or not _REF.fullmatch(check.ref) or ".." in check.ref:
            raise Refused("contains needs a ref under refs/heads, refs/remotes or refs/tags")
        return ["git", "for-each-ref", "--format=%(refname)", f"--contains={rev}", check.ref]
    raise Refused(
        f"git check {check.op!r} is not a template (rev-parse, log, show-file, ls-tree, contains)"
    )


def plan_gh(check: GhCheck, repo: str) -> list[str]:
    if not _REPO.fullmatch(repo or ""):
        raise Refused(f"repo {repo!r} is not owner/name")
    number = check.number
    if check.op in ("pr-view", "pr-checks") and (
        not isinstance(number, int) or isinstance(number, bool) or not 0 < number < 10**7
    ):
        raise Refused("a pr check needs a PR number")
    if check.op == "pr-view":
        fields = list(check.fields)
        if not fields or any(f not in _PR_FIELDS for f in fields):
            raise Refused(f"pr-view fields must be from: {', '.join(sorted(_PR_FIELDS))}")
        return ["gh", "pr", "view", str(number), "-R", repo, "--json", ",".join(fields)]
    if check.op == "pr-checks":
        return ["gh", "pr", "checks", str(number), "-R", repo]
    if check.op == "run-list":
        if not isinstance(check.sha, str) or not _SHA.fullmatch(check.sha):
            raise Refused("run-list needs a commit sha (7 to 40 lowercase hex)")
        return ["gh", "run", "list", "-R", repo, "--commit", check.sha, "--limit", "50",
                "--json", "name,workflowName,status,conclusion,headSha"]  # fmt: skip
    if check.op == "api":
        segments = (check.path if isinstance(check.path, str) else "").split("/")
        if not 0 < len(segments) <= 8 or any(
            not _API_SEGMENT.fullmatch(s) or s in (".", "..") for s in segments
        ):
            raise Refused("api path must be 1 to 8 plain segments under repos/<repo>/")
        head = segments[0] if segments[0] != "actions" else "/".join(segments[:2])
        if head not in _API_HEADS:
            raise Refused(f"api may read only: {', '.join(sorted(_API_HEADS))}")
        return ["gh", "api", f"repos/{repo}/{'/'.join(segments)}"]
    raise Refused(f"gh check {check.op!r} is not a template (pr-view, pr-checks, run-list, api)")


def plan_journal(check: JournalCheck) -> list[str]:
    if not _UNIT.fullmatch(check.unit):
        raise Refused("journal unit must be a plain unit name")
    if not _SINCE.fullmatch(check.since):
        raise Refused("journal since must be a date or a -N(s|min|h|d) offset")
    if not check.pattern or len(check.pattern) > _PATTERN_MAX or "\0" in check.pattern:
        raise Refused("journal pattern must be 1 to 512 characters")
    return ["journalctl", "--user", "--no-pager", "-o", "cat", "-u", check.unit,
            "--since", check.since, f"--grep={check.pattern}"]  # fmt: skip


def plan_grep(check: GrepCheck, repo_root: Path, env: dict) -> list[str]:
    if not check.pattern or len(check.pattern) > _PATTERN_MAX or "\0" in check.pattern:
        raise Refused("grep pattern must be 1 to 512 characters")
    if not check.path.startswith(("/", "~")):
        path = "." if check.path == "." else _repo_path(check.path, repo_root, env)
        return ["git", "grep", "-n", "-E", "-e", check.pattern, "--", path]
    logs = logs_dir().resolve()
    target = Path(check.path).expanduser().resolve()
    if not target.is_relative_to(logs) or not target.is_file():
        raise Refused(f"grep outside the repo reads only a file under {logs}")
    return ["grep", "-n", "-E", "-e", check.pattern, "--", str(target)]


def _env(home: str, kind: str, index: str | None) -> dict:
    env = {
        "PATH": _SAFE_PATH, "HOME": home, "LANG": "C.UTF-8", "PAGER": "cat", "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_SOURCE": _EMPTY_TREE, "GIT_CONFIG_COUNT": str(len(_GIT_PINS)),
    }  # fmt: skip
    for i, (k, v) in enumerate(_GIT_PINS):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = k, v
    if index:
        env["GIT_INDEX_FILE"] = index
    if kind == "gh":
        # No GH_TOKEN: gh authenticates from its config (MEASURED under env -i),
        # and a passed token is one --jq away from stdout. The config is a COPY
        # in the temp HOME (see _gh_config_copy), so gh writes no real state.
        env["GH_CONFIG_DIR"] = str(Path(home) / "gh")
        env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", NO_COLOR="1")
    if kind == "journal" and os.environ.get("XDG_RUNTIME_DIR"):
        env["XDG_RUNTIME_DIR"] = os.environ["XDG_RUNTIME_DIR"]
    return env


def _index_copy(repo_root: Path, home: str, env: dict) -> str | None:
    """A copy of the repo's index under ``home``, so a git read that refreshes
    the index writes the copy. None when the repo has no index yet."""
    out = subprocess.run(
        ["/usr/bin/git", "rev-parse", "--path-format=absolute", "--git-path", "index"],
        cwd=repo_root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    src = Path(out.stdout.strip())
    if out.returncode != 0 or not src.is_file():
        return None
    dst = Path(home) / "index"
    shutil.copyfile(src, dst)
    return str(dst)


def _drain(stream, cap: int, sink: dict) -> None:
    digest, head, total = hashlib.sha256(), bytearray(), 0
    for chunk in iter(lambda: stream.read(65536), b""):
        digest.update(chunk)
        total += len(chunk)
        if len(head) < cap:
            head += chunk[: cap - len(head)]
    sink.update(head=bytes(head), sha=digest.hexdigest(), total=total)


def _kill_group(pid: int) -> None:
    # start_new_session makes the child its group's leader, so pgid == pid. Never
    # 1 or 0: killpg(1) signals every process this user owns.
    if pid > 1:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGKILL)


def _execute(
    argv: list[str], cwd: Path, env: dict, timeout_s: float
) -> tuple[int | None, bool, dict, dict]:
    proc = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, start_new_session=True,
    )  # fmt: skip
    out: dict = {}
    err: dict = {}
    drains = [
        threading.Thread(target=_drain, args=(proc.stdout, STDOUT_CAP, out), daemon=True),
        threading.Thread(target=_drain, args=(proc.stderr, STDERR_CAP, err), daemon=True),
    ]
    for t in drains:
        t.start()
    deadline, timed_out = time.monotonic() + timeout_s, False
    try:
        # Wait WITHOUT reaping, so the leader's pid (and with it the group id)
        # cannot be reused before the group is killed below.
        while os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT | os.WNOHANG) is None:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(0.02)
    finally:
        # Always, not only on a timeout: a grandchild still holding a pipe would
        # keep the drains from ever reaching EOF.
        _kill_group(proc.pid)
        proc.wait()
    for t in drains:
        t.join(timeout=10)
    return (None if timed_out else proc.returncode), timed_out, out, err


def _plan(check, repo_root: Path, repo: str, env: dict) -> list[str]:
    if isinstance(check, GitCheck):
        return plan_git(check, repo_root, env)
    if isinstance(check, GhCheck):
        return plan_gh(check, repo)
    if isinstance(check, JournalCheck):
        return plan_journal(check)
    if isinstance(check, GrepCheck):
        return plan_grep(check, repo_root, env)
    raise Refused(f"unknown check kind {type(check).__name__}")


def _run_sql(check: SqlCheck, db_path: Path) -> CheckRun:
    from genesis.db.readonly_query import run_readonly_query

    command = ("sql", check.sql)
    try:
        res = run_readonly_query(db_path, check.sql, tuple(check.params), max_rows=SQL_MAX_ROWS)
    except Exception as exc:  # noqa: BLE001 - a failed read is a failed check, recorded
        return CheckRun(
            "sql", command, exit_code=1, stderr_head=f"{type(exc).__name__}: {exc}"[:STDERR_CAP]
        )
    lines = [json.dumps(res.columns, default=str)] + [
        json.dumps(list(r), default=str) for r in res.rows
    ]
    text = "\n".join(lines).encode()
    return CheckRun(
        "sql", command, exit_code=0, stdout_head=text[:STDOUT_CAP].decode(errors="replace"),
        stdout_sha256=hashlib.sha256(text).hexdigest(),
        stdout_truncated=res.truncated or len(text) > STDOUT_CAP,
    )  # fmt: skip


def run(
    check, *, repo_root: Path, db_path: Path, repo: str, timeout_s: float = TIMEOUT_S
) -> CheckRun:
    """Run ``check`` read-only and return what it printed; refuse what is not.

    Never raises: a refusal, a malformed field, or a failure while preparing the
    check (the index copy, the ignore check) comes back as ``refused``."""
    if isinstance(check, SqlCheck):
        return _run_sql(check, db_path)
    kind = type(check).__name__.removesuffix("Check").lower()
    try:
        home = tempfile.mkdtemp(prefix="close-check-", dir=_big_tmp())
    except Exception as exc:  # noqa: BLE001 - the contract is a CheckRun, never a raise
        return CheckRun(kind, (), refused=f"could not prepare the check: {type(exc).__name__}")
    try:
        repo_root = Path(repo_root).resolve()
        if kind == "gh":
            _gh_config_copy(home)
        env = _env(home, kind, None)
        env = _env(home, kind, _index_copy(repo_root, home, env))
        argv = _plan(check, repo_root, repo, env)
        exe = shutil.which(argv[0], path=_SAFE_PATH)
        if exe is None:
            return CheckRun(
                kind, tuple(argv), refused=f"{argv[0]} is not installed in {_SAFE_PATH}"
            )
        code, timed_out, out, err = _execute([exe, *argv[1:]], repo_root, env, timeout_s)
        return CheckRun(
            kind, tuple(argv), exit_code=code, timed_out=timed_out,
            stdout_head=out.get("head", b"").decode(errors="replace"),
            stdout_sha256=out.get("sha", ""), stdout_truncated=out.get("total", 0) > STDOUT_CAP,
            stderr_head=err.get("head", b"").decode(errors="replace"),
        )  # fmt: skip
    except Refused as exc:
        return CheckRun(kind, (), refused=str(exc))
    except Exception as exc:  # noqa: BLE001 - a malformed field (wrong type, a lone surrogate) or a prep failure
        return CheckRun(
            kind, (), refused=f"could not run the check: {type(exc).__name__}: {exc}"[:500]
        )
    finally:
        shutil.rmtree(home, ignore_errors=True)


def _big_tmp() -> str:
    from genesis.util.tmp import big_tmp_dir

    return big_tmp_dir()


def _gh_config_copy(home: str) -> None:
    """Copy gh's config into the temp HOME, so a read that refreshes gh's state
    files writes the copy, which is removed with the HOME."""
    src = Path(os.environ.get("GH_CONFIG_DIR") or Path.home() / ".config" / "gh")
    if src.is_dir():
        shutil.copytree(src, Path(home) / "gh")


def matches(result: CheckRun, expect: str) -> bool:
    """Whether ``expect`` matches what the check printed. The pattern is
    subagent-written, so it runs with a time limit; running out, or a pattern
    that does not compile, is no match."""
    try:
        return regex.search(expect, result.stdout_head, timeout=EXPECT_TIMEOUT_S) is not None
    except (TimeoutError, regex.error, TypeError):
        return False
