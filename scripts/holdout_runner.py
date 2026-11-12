#!/usr/bin/env python3
"""Run an install's hidden holdout scenarios against a merged PR.

A holdout is a small set of exact behavioural checks that builders never see,
so a builder cannot make its own tests agree with its code instead of with the
intent. The scenarios live outside the repository, by default in
``$GENESIS_HOME/holdout/HOLDOUT.md`` (``~/.genesis`` when unset); ``docs/reference/holdout.md`` documents the
format and the rules. This script is the only reader a validator needs: it
prints scenario ids, verdicts and each scenario's one-line ``Protects`` text,
never its steps or its expected output, so running it does not spoil the set.

``--pr N`` judges a merged PR. Each scenario whose ``Touches`` globs match a
file the merge changed runs twice, in detached temporary worktrees: at the
merge commit's first parent (the default branch just before the merge) and at
the merge commit. Running both is what attributes a result to THIS PR.

Regression scenarios (``Expect-before: PASS``):

    parent  merge   verdict
    pass    pass    PASS
    pass    fail    REGRESSED        this PR's failure
    fail    pass    FIXED
    fail    fail    BROKEN-BEFORE    a defect on the default branch, not this PR

Intent scenarios (``Expect-before: FAIL``) name the issue they were written
for (``Issue: N``). They are judged against a PR only when that PR links the
issue: GitHub lists it as closed by the PR, or the body says ``closes``,
``fixes`` or ``resolves`` ``#N`` (GitHub's closing keywords; ``part of`` does
not link, because under a split only the last PR delivers the intent). A linked intent
scenario runs whatever files the PR touched, because where the builder put the
change is not something the writer could know. Any other PR is never charged
with not delivering someone else's work:

    linked  parent  merge   verdict
    any     pass    any     STALE-INTENT     the scenario is wrong, or it landed elsewhere
    yes     fail    pass    DELIVERED
    yes     fail    fail    NOT-DELIVERED    this PR's failure
    no      fail    pass    DELIVERED-UNLINKED
    no      fail    fail    PENDING          not this PR's work

``--at-path DIR`` runs scenarios against a directory and prints PASS or FAIL
per id. A scenario writer uses it to check that a new scenario passes on the
current tree and fails on a deliberately broken copy.

Steps run with ``bash`` in the checkout under test (``HOLDOUT_ROOT``), with
that checkout's ``src`` first on ``PYTHONPATH`` so ``import genesis`` loads the
code under test and not an editable install of another checkout, and with
``HOLDOUT_PYTHON`` and ``PATH`` pointing at this script's own interpreter. The
environment is an allowlist (locale, terminal and user names; no tokens, no SSH
agent), HOME, TMPDIR, the Genesis home and database path are temporary, Qdrant
points at an unroutable address, and files a step writes are capped at 64 MiB.
Whatever a step leaves running is killed when it ends. There is no network
isolation: a step that reaches for the network can still get it, which the
format forbids.

Exit status: 0 nothing attributable failed (including "no holdout on this
install"), 1 a REGRESSED or NOT-DELIVERED verdict, 2 usage error, 3 the run
could not be completed (the reason is printed).
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path

# The repository's own resolvers, not copies of them: a copy of genesis_home lost
# its ~ expansion and a copy of the scratch rule fell back to the default temp.
# This checkout's src goes first (the scripts/pr_verification.py pattern), so the
# runner imports its own copy under any interpreter: both modules are stdlib-only.
SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from genesis.env import genesis_home  # noqa: E402
from genesis.util.tmp import big_tmp_dir  # noqa: E402

DEFAULT_HOLDOUT = genesis_home() / "holdout" / "HOLDOUT.md"
# The holdout fence writes only real exposures (reads, writes, accesses and
# content seen by anyone but the writer) here, and never rotates it.
EXPOSURE_LOG = genesis_home() / "logs" / "holdout-exposures.jsonl"
# A proxy for "the fence is installed": the hook file is present in this checkout.
# The wiring itself lives in .claude/settings.json and .codex/config.toml.
FENCE_HOOK = Path(__file__).resolve().parent / "hooks" / "holdout_fence.py"

# A holdout step is a deterministic, local check that normally finishes in
# well under a second. 120 s is two orders of magnitude above that; past it the
# step is hung, and a hung step must not hold a validator pass indefinitely.
STEP_TIMEOUT_S = 120

# gh: the same bound gh_pr_metadata uses (pr_recording.py) for one read query.
GH_TIMEOUT_S = 120
# git: a fetch of one commit or a worktree checkout of this repository finishes in
# seconds; ten minutes is far past that and still ends a hung network read.
GIT_TIMEOUT_S = 600

# Observed output from a failing arm is shown so the failure can be acted on.
# It is bounded, and the omission is stated, never silent.
OBSERVED_CHARS = 400

# Port 9 (discard) on loopback: a Qdrant client gets a refused connection
# instead of the live collection.
UNROUTABLE_QDRANT = "http://127.0.0.1:9"

# Only this much of a step's output is read; an expectation must fit in it.
STDOUT_LIMIT_BYTES = 1024 * 1024
# ulimit -f, in KiB: the most any one file a step writes (its stdout included) may grow.
STEP_FILE_LIMIT_KIB = 64 * 1024

_SCENARIO_HEADING = re.compile(r"^## (H-\d+)(?:[ \t]+(.*))?$")
_SCENARIO_LIKE = re.compile(r"^## H-\S*")
_ANY_HEADING = re.compile(r"^#{1,6}[ \t]")
_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_FIELD = re.compile(r"^([A-Z][A-Za-z-]*):[ \t]*(.*)$")
_FENCE = re.compile(r"^(```+|~~~+)")
# The only inherited variables a step sees. Everything else, credentials and the
# SSH agent included, is dropped rather than filtered by name.
_INHERITED_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "USER", "LOGNAME", "SHELL")
# GitHub's closing keywords only. "Part of #N" and "implements #N" do not link:
# under a split, every PR but the last is part of the issue and cannot deliver
# its intent, and charging each would open a false fail-intent per PR.
_CLOSING_REF = re.compile(r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[:\s]+#(\d+)\b")


class RunnerError(Exception):
    """The run cannot be completed; the message says why."""


@dataclass
class Scenario:
    sid: str
    title: str
    fields: dict[str, str] = field(default_factory=dict)
    steps: str | None = None
    expect: str | None = None
    problem: str | None = None

    @property
    def protects(self) -> str:
        return self.fields.get("Protects", "").strip() or "(no Protects line)"

    @property
    def touches(self) -> list[str]:
        raw = self.fields.get("Touches", "")
        return [t.strip() for t in raw.split(",") if t.strip()]

    @property
    def expect_before(self) -> str:
        return self.fields.get("Expect-before", "").strip().upper()

    @property
    def expect_exit(self) -> int:
        return int(self.fields.get("Expect-exit", "0").strip())

    @property
    def author_date(self) -> dt.date | None:
        """The earliest YYYY-MM-DD on the Author line, or None when there is none.

        The earliest, because a later date on the line cannot move back the day
        the scenario first existed. Any date that does not parse gives None.
        """
        found = _DATE.findall(self.fields.get("Author", ""))
        try:
            dates = [dt.date.fromisoformat(d) for d in found]
        except ValueError:
            return None
        return min(dates) if dates else None

    @property
    def author_dates(self) -> list[dt.date]:
        """Every real YYYY-MM-DD on the Author line: days the writer worked on it."""
        dates = []
        for found in _DATE.findall(self.fields.get("Author", "")):
            with contextlib.suppress(ValueError):
                dates.append(dt.date.fromisoformat(found))
        return dates

    @property
    def issue(self) -> int:
        return int(self.fields.get("Issue", "").strip().lstrip("#"))


def parse_holdout(text: str) -> list[Scenario]:
    """Parse HOLDOUT.md into scenarios. Malformed ones carry ``problem``."""
    scenarios: list[Scenario] = []
    current: Scenario | None = None
    pending_block: str | None = None  # "Steps" or "Expect", awaiting a fence
    fence: str | None = None  # the open fence, when inside a code block
    keep_block = False  # whether the open block is a Steps/Expect body
    block: list[str] = []
    # Split on "\n" only: str.splitlines() also breaks on form feeds, NEL and
    # Unicode separators, which a scenario may need to carry literally.
    for line in (ln[:-1] if ln.endswith("\r") else ln for ln in text.split("\n")):
        if fence is not None and not keep_block and _SCENARIO_LIKE.match(line):
            # A stray fence never closed: end it here rather than let it swallow
            # this scenario, and mark the scenario it sat in.
            if current is not None:
                current.problem = "code block not closed"
            fence, pending_block, keep_block, block = None, None, False, []
        if fence is not None:
            if line.strip() == fence:
                if keep_block and current is not None:
                    body = "\n".join(block)
                    if pending_block == "Steps":
                        current.steps = body
                    else:
                        current.expect = body
                fence, pending_block, keep_block, block = None, None, False, []
            else:
                block.append(line)
            continue
        opened = _FENCE.match(line)
        if opened:
            # A fence outside a Steps/Expect slot is still a code block: its
            # contents are never read as headings or fields.
            fence, block = opened.group(1), []
            keep_block = current is not None and pending_block is not None
            continue
        heading = _SCENARIO_HEADING.match(line)
        if heading:
            current = Scenario(sid=heading.group(1), title=(heading.group(2) or "").strip())
            scenarios.append(current)
            pending_block = None
            continue
        if _SCENARIO_LIKE.match(line):
            # "## H-02a" or "## H-01-retired": not an id this format allows.
            bad = Scenario(
                sid=line[3:].split()[0], title="", problem="heading is not '## H-<digits> title'"
            )
            scenarios.append(bad)
            current, pending_block = None, None
            continue
        if _ANY_HEADING.match(line):
            # Any other heading ends the scenario, so its lines cannot leak into it.
            current, pending_block = None, None
            continue
        if current is None:
            continue
        named = _FIELD.match(line)
        if named:
            key, value = named.group(1), named.group(2)
            if key in ("Steps", "Expect") and not value:
                pending_block = key
            else:
                current.fields[key] = value
                pending_block = None
    if fence is not None and current is not None:
        current.problem = "unclosed code block"
    for sc in scenarios:
        if not sc.problem:
            sc.problem = _validate(sc)
    counts: dict[str, int] = {}
    for sc in scenarios:
        counts[sc.sid] = counts.get(sc.sid, 0) + 1
    for sc in scenarios:
        if counts[sc.sid] > 1:
            sc.problem = "duplicate id"
    return scenarios


def _validate(sc: Scenario) -> str | None:
    if sc.steps is None:
        return "no Steps block"
    if sc.expect is None:
        return "no Expect block"
    if not sc.touches:
        return "no Touches line"
    if sc.expect_before not in ("PASS", "FAIL"):
        return "Expect-before must be PASS or FAIL"
    try:
        sc.expect_exit  # noqa: B018 - validate it parses
    except ValueError:
        return "Expect-exit is not an integer"
    if sc.expect_before == "FAIL":
        try:
            sc.issue  # noqa: B018
        except ValueError:
            return "an intent scenario needs an Issue: line with the issue number"
    return None


@dataclass
class Arm:
    passed: bool
    exit_code: int | None
    stdout: str
    stdout_matched: bool
    note: str = ""


def step_env(root: Path, home: Path, tmp: Path, marker: str = "") -> dict[str, str]:
    """The environment a step runs in: the code under test, nothing live."""
    env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}
    if marker:
        env[_MARKER_ENV] = marker
    interpreter = Path(sys.executable)
    base_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update(
        HOME=str(home),
        TMPDIR=str(tmp),
        HOLDOUT_ROOT=str(root),
        HOLDOUT_PYTHON=str(interpreter),
        PATH=f"{interpreter.parent}{os.pathsep}{base_path}",
        PYTHONPATH=str(root / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        GENESIS_REPO_ROOT=str(root),
        GENESIS_HOME=str(home / ".genesis"),
        GENESIS_DB_PATH=str(tmp / "genesis.db"),
        QDRANT_URL=UNROUTABLE_QDRANT,
    )
    return env


def run_scenario(sc: Scenario, root: Path) -> Arm:
    """Run one scenario's steps in ``root`` and compare with its expectation."""
    if sc.steps is None or sc.expect is None:
        raise RunnerError(f"{sc.sid} is malformed and cannot run")
    holder = Path(tempfile.mkdtemp(prefix="holdout-step-", dir=_scratch_base()))
    try:
        home = holder / "home"
        tmp = holder / "tmp"
        home.mkdir()
        tmp.mkdir()
        script = holder / "steps.sh"
        script.write_text(f"ulimit -f {STEP_FILE_LIMIT_KIB}\n{sc.steps}\n")
        out_path = holder / "stdout"
        marker = uuid.uuid4().hex
        with open(out_path, "wb") as out_file:
            proc = subprocess.Popen(
                ["bash", str(script)],
                cwd=str(root),
                env=step_env(root, home, tmp, marker),
                stdin=subprocess.DEVNULL,
                stdout=out_file,
                stderr=subprocess.DEVNULL,
                start_new_session=True,  # its own process group, killed below
            )
            try:
                proc.wait(timeout=STEP_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                _kill_group(proc)
                return Arm(False, None, "", False, f"timed out after {STEP_TIMEOUT_S}s")
            finally:
                _kill_group(proc)  # whatever the step left running in its group
                _kill_marked(marker)  # and anything that left the group
        with open(out_path, "rb") as fh:
            raw = fh.read(STDOUT_LIMIT_BYTES + 1)
        if len(raw) > STDOUT_LIMIT_BYTES:
            return Arm(False, proc.returncode, "", False, f"stdout over {STDOUT_LIMIT_BYTES} bytes")
        stdout = raw.decode("utf-8", errors="replace")
        matched = stdout.rstrip("\n") == sc.expect.rstrip("\n")
        return Arm(matched and proc.returncode == sc.expect_exit, proc.returncode, stdout, matched)
    finally:
        shutil.rmtree(holder, ignore_errors=True)


def _kill_group(proc: subprocess.Popen) -> None:
    # The step leads its own session, so its pid is its process group id. Never
    # signal group 1 or below: killpg(1) would reach every process of this user.
    if proc.pid > 1:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


_MARKER_ENV = "HOLDOUT_STEP_MARKER"


def _kill_marked(marker: str) -> None:
    """Kill every process still carrying this step's marker in its environment.

    A descendant that starts its own session (``start_new_session=True``, which
    code in this repository does) leaves the step's process group, so the group
    kill misses it. It still inherits the environment, so it is found by the
    marker. One that clears its environment on purpose is not found; that is
    outside what a scenario's code does by accident.
    """
    needle = f"{_MARKER_ENV}={marker}".encode()
    me = os.getpid()
    for _ in range(3):  # a child may fork while the first sweep runs
        found = False
        try:
            entries = os.listdir("/proc")
        except OSError:
            return
        for entry in entries:
            if not entry.isdigit():
                continue
            pid = int(entry)
            if pid <= 1 or pid == me:
                continue
            try:
                with open(f"/proc/{pid}/environ", "rb") as fh:
                    environ = fh.read()
            except OSError:
                continue
            if needle in environ.split(b"\0"):
                found = True
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        if not found:
            return


def describe_failed_arm(arm: Arm) -> str:
    """Say what a FAILING arm did, without ever echoing the expected output."""
    if arm.note:
        return arm.note
    if arm.stdout_matched:
        # The stdout equals the expectation here, so printing it would reveal it.
        return f"stdout as expected, exit={arm.exit_code} (wrong exit status)"
    out = arm.stdout.rstrip("\n")
    if len(out) > OBSERVED_CHARS:
        omitted = len(out) - OBSERVED_CHARS
        out = out[:OBSERVED_CHARS] + f" [{omitted} more chars omitted]"
    return f"exit={arm.exit_code}, stdout={json.dumps(out)}"


def judge(sc: Scenario, parent: Arm, merge: Arm, linked: set[int]) -> tuple[str, Arm | None]:
    """Return the verdict and the arm whose failure to describe, if any."""
    if sc.expect_before == "PASS":
        if parent.passed and merge.passed:
            return "PASS", None
        if parent.passed:
            return "REGRESSED", merge
        if merge.passed:
            return "FIXED", None
        return "BROKEN-BEFORE", merge
    if parent.passed:
        return "STALE-INTENT", None
    is_linked = sc.issue in linked
    if merge.passed:
        return ("DELIVERED" if is_linked else "DELIVERED-UNLINKED"), None
    return ("NOT-DELIVERED", merge) if is_linked else ("PENDING", None)


ATTRIBUTABLE_FAILURES = frozenset({"REGRESSED", "NOT-DELIVERED"})


def issue_closed(issue: int, repo: str | None, root: Path) -> bool | None:
    """Whether the issue is closed: True, False, or None when it cannot be read."""
    try:
        info = gh_json(
            ["issue", "view", str(issue), *(["--repo", repo] if repo else []), "--json", "state"],
            root,
        )
    except (RunnerError, ValueError):
        return None
    state = str(info.get("state") or "").upper()
    return {"CLOSED": True, "OPEN": False}.get(state)


def touched(sc: Scenario, files: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(f, pat) for pat in sc.touches for f in files)


def _scratch_base() -> str:
    """The disk-backed scratch root (``genesis.util.tmp.big_tmp_dir``), created when missing.

    Two full worktree checkouts land here, so it must never fall back to the
    default temp location, which may be a small tmpfs or a session's quota-capped
    working temp. If it cannot be created, the run stops instead.
    """
    try:
        return big_tmp_dir()
    except OSError as exc:
        raise RunnerError(f"cannot create the scratch directory: {exc}") from exc


def _git(root: Path, *args: str) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",  # a path's bytes survive whatever the locale
            check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(f"git {' '.join(args)} timed out after {GIT_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        raise RunnerError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def gh_json(args: list[str], cwd: Path) -> dict:
    """Run a read-only ``gh`` query in ``cwd`` and parse its JSON. Tests replace this."""
    try:
        proc = subprocess.run(
            ["gh", *args],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(cwd),
            timeout=GH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunnerError(f"gh {' '.join(args[:2])} timed out after {GH_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        raise RunnerError(f"gh {' '.join(args[:2])} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def resolve_merge(pr: int, repo: str | None, root: Path) -> tuple[str, set[int]]:
    """Return the PR's merge commit and the issues it links.

    Refuses anything not merged into the default branch. The first parent of a
    squash merge (the only merge method this repository allows) is the default
    branch just before the merge; under a rebase merge it would be a commit
    inside the PR, and the parent arm would no longer mean "before".
    """
    repo_args = ["--repo", repo] if repo else []
    info = gh_json(
        [
            "pr",
            "view",
            str(pr),
            *repo_args,
            "--json",
            "state,mergeCommit,baseRefName,body,closingIssuesReferences",
        ],
        root,
    )
    if info.get("state") != "MERGED" or not (info.get("mergeCommit") or {}).get("oid"):
        raise RunnerError(f"PR #{pr} is not merged")
    default = gh_json(
        ["repo", "view", *([repo] if repo else []), "--json", "defaultBranchRef,nameWithOwner"],
        root,
    )
    default_name = (default.get("defaultBranchRef") or {}).get("name")
    this_repo = str(default.get("nameWithOwner") or "").lower()
    if info.get("baseRefName") != default_name:
        raise RunnerError(
            f"PR #{pr} merged into {info.get('baseRefName')}, not the default branch "
            f"{default_name}: its first parent is not the state it was built against"
        )
    # A closing reference can name another repository's issue (Closes owner/other#5);
    # its number must not link a same-numbered issue here.
    linked = {
        int(ref["number"])
        for ref in info.get("closingIssuesReferences") or []
        if isinstance(ref, dict)
        and str(ref.get("number", "")).isdigit()
        and this_repo
        and _ref_repo(ref).lower() == this_repo
    }
    linked |= {int(n) for n in _CLOSING_REF.findall(info.get("body") or "")}
    return info["mergeCommit"]["oid"], linked


def _ref_repo(ref: dict) -> str:
    """``owner/name`` of a ``closingIssuesReferences`` entry, or '' if malformed.

    The same shape ``session_awareness/repo_pulse.py`` reads.
    """
    repository = ref.get("repository") or {}
    owner = (repository.get("owner") or {}).get("login") or ""
    name = repository.get("name") or ""
    return f"{owner}/{name}" if owner and name else ""


class _Worktree:
    """A detached worktree at ``sha``, removed by its exact path on exit."""

    def __init__(self, root: Path, sha: str, holder: Path, name: str) -> None:
        self.root, self.sha, self.path = root, sha, holder / name

    def __enter__(self) -> Path:
        _git(self.root, "worktree", "add", "--detach", "--quiet", str(self.path), self.sha)
        return self.path

    def __exit__(self, *exc: object) -> None:
        subprocess.run(
            ["git", "-C", str(self.root), "worktree", "remove", "--force", str(self.path)],
            capture_output=True,
            check=False,
        )


def load(holdout: Path) -> list[Scenario] | None:
    if not holdout.is_file():
        return None
    return parse_holdout(holdout.read_text(encoding="utf-8"))


def cmd_pr(pr: int, repo: str | None, root: Path, holdout: Path, out=None) -> int:
    out = out or sys.stdout
    scenarios = load(holdout)
    if scenarios is None:
        print("no holdout on this install", file=out)
        return 0
    if holdout.resolve() == DEFAULT_HOLDOUT.resolve():
        # Only for the install's own holdout, never a file passed for testing.
        _ensure_ignore(holdout.parent)
        _print_exposures(out, scenarios)
    merge, linked = resolve_merge(pr, repo, root)
    try:
        _git(root, "cat-file", "-e", f"{merge}^{{commit}}")
    except RunnerError:
        _git(root, "fetch", "--quiet", "origin", merge)
    parent = _git(root, "rev-parse", f"{merge}^1").strip()
    # --no-renames: a moved file lists both paths, so a scenario on the old
    # path still runs when its code is moved away. -z: names come unquoted and
    # NUL-separated, so a name with non-ASCII bytes or a newline stays intact.
    changed = _git(root, "diff", "--name-only", "-z", "--no-renames", parent, merge)
    files = [f for f in changed.split("\0") if f]
    for sc in scenarios:
        if sc.problem:
            print(f"{sc.sid} MALFORMED — {sc.problem}", file=out)
    runnable = [
        sc
        for sc in scenarios
        if not sc.problem
        and (touched(sc, files) or (sc.expect_before == "FAIL" and sc.issue in linked))
    ]
    skipped = sum(1 for sc in scenarios if not sc.problem) - len(runnable)
    print(
        f"holdout: PR #{pr} merge {merge[:12]} parent {parent[:12]}; "
        f"{len(runnable)} scenario(s) apply to it ({len(files)} changed file(s)), "
        f"{skipped} do not",
        file=out,
    )
    if not runnable:
        return 0
    worst = 0
    holder = Path(tempfile.mkdtemp(prefix="holdout-run-", dir=_scratch_base()))
    try:
        with (
            _Worktree(root, parent, holder, "parent") as p_root,
            _Worktree(root, merge, holder, "merge") as m_root,
        ):
            for sc in runnable:
                verdict, failed = judge(
                    sc, run_scenario(sc, p_root), run_scenario(sc, m_root), linked
                )
                if verdict == "PENDING" and issue_closed(sc.issue, repo, root):
                    # Its issue was closed without any linked PR delivering it:
                    # an unlinked build that missed the intent, or a wrong
                    # scenario. Not attributable to this PR.
                    verdict = "CLOSED-NOT-DELIVERED"
                line = f"{sc.sid} {verdict} — {sc.protects}"
                if failed is not None:
                    line += f" — observed {describe_failed_arm(failed)}"
                print(line, file=out)
                if verdict in ATTRIBUTABLE_FAILURES:
                    worst = 1
    finally:
        shutil.rmtree(holder, ignore_errors=True)
        # A removal that failed leaves a registration whose directory is gone.
        subprocess.run(
            ["git", "-C", str(root), "worktree", "prune"], capture_output=True, check=False
        )
    return worst


def _ensure_ignore(holdout_dir: Path) -> None:
    """Keep ripgrep-based searches (rg, the Grep tool) out of the holdout.

    ripgrep reads ``.ignore`` files in the directories it searches, so a search
    of ``~/.genesis`` or ``~`` skips a directory listed there. Idempotent; a
    failure to write it is not a reason to stop judging.
    """
    ignore = holdout_dir.parent / ".ignore"
    entry = f"{holdout_dir.name}/"
    try:  # OSError, or ValueError for a file that is not UTF-8
        lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
        if entry not in (ln.strip() for ln in lines):
            with open(ignore, "a", encoding="utf-8") as fh:
                if lines and not ignore.read_text(encoding="utf-8").endswith("\n"):
                    fh.write("\n")
                fh.write(entry + "\n")
    except (OSError, ValueError):
        pass


def _utc_day(value: object) -> dt.date | None:
    """The UTC day of an ISO timestamp, or None when it is not one."""
    try:
        when = dt.datetime.fromisoformat(str(value))
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        return when.astimezone(dt.UTC).date()
    except (ValueError, OverflowError):
        return None


def _print_exposures(out, scenarios: list[Scenario]) -> None:
    """Always one line: how many exposures the holdout fence has recorded.

    Silence would read as "none", so a missing or unreadable log says so, and
    lines that do not parse are counted rather than ending the count. When there
    are exposures, the line names the scenarios they may have spoiled: those
    whose Author date is no later than the day after the latest exposure. Dates
    are whole days, so that day's margin covers time zones.

    The log is append-only, so a row with no readable time, or a line that does
    not parse, happened no later than the next dated row: that row's day bounds
    it, unless it is older than a day already seen (the clock went back). Only
    such a row at the end of the log has no bound, and then every scenario counts
    as spoiled. So does a scenario with no Author date, or one dated later than
    tomorrow, which cannot be right.

    An access declaring the holdout-writer agent is only counted when it is no
    later than the day after the newest Author date in the file: the writer has
    written a scenario since. A later one, one with no readable time, or any at
    all when no scenario carries a date, is reported for the owner to confirm,
    because the writer is known only by the name an agent declares. So the
    message clears itself the next time the writer writes a scenario, for as
    long as that scenario stays in the file. A row with no readable time never
    clears; the fence always writes one.
    """
    if not FENCE_HOOK.exists():
        print("holdout fence: not installed in this checkout; exposures are not recorded", file=out)
        return
    if not EXPOSURE_LOG.exists():
        print("holdout fence: no exposures logged (no exposure log yet)", file=out)
        return
    count, bad, times, days = 0, 0, [], []
    unbounded = False  # an exposure of unknown time with no dated row after it yet
    writer_rows: list[tuple[str, dt.date | None]] = []
    try:
        with open(EXPOSURE_LOG, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    row = None
                if not isinstance(row, dict):
                    bad += 1
                    unbounded = True
                    continue
                day = _utc_day(row.get("at"))
                if day is not None and unbounded and (not days or day >= max(days)):
                    # A later dated row bounds it, unless the clock went back.
                    days.append(day)
                    unbounded = False
                if row.get("kind") == "allowed":
                    # The writer is known only by the name an agent declares, so
                    # these are matched against the scenarios below, never
                    # treated as harmless outright.
                    writer_rows.append((str(row.get("at") or ""), day))
                    continue
                count += 1
                times.append(str(row.get("at") or ""))
                if day is None:
                    unbounded = True
                else:
                    days.append(day)
    except OSError as exc:
        print(f"holdout fence: exposure log unreadable ({exc}); treat as unknown", file=out)
        return
    tail = f"; {bad} unreadable line(s)" if bad else ""
    tomorrow = dt.datetime.now(dt.UTC).date() + dt.timedelta(days=1)
    # A date past tomorrow cannot be right, and would silence every later access.
    written = [d for sc in scenarios for d in sc.author_dates if d <= tomorrow]
    newest = max(written) if written else None
    unmatched = [
        (at, day)
        for at, day in writer_rows
        if day is None or newest is None or day > newest + dt.timedelta(days=1)
    ]
    if unmatched:
        dated = [at for at, day in unmatched if day is not None]
        when = f"latest {max(dated)}" if dated else "time unreadable"
        since = "since the newest scenario was written" if newest else "and no dated scenario"
        tail += (
            f"; {len(unmatched)} of {len(writer_rows)} access(es) declaring the holdout-writer "
            f"agent {since}, {when}: confirm each was a writer run you ordered"
        )
    elif writer_rows:
        tail += (
            f"; {len(writer_rows)} holdout-writer access(es), none later than the newest scenario"
        )
    if not count and not bad:
        print(f"holdout fence: no exposures logged{tail}", file=out)
        return
    latest = None if unbounded or not days else max(days)
    spoiled = [
        sc.sid
        for sc in scenarios
        if latest is None
        or sc.author_date is None
        or sc.author_date > tomorrow
        or sc.author_date <= (latest + dt.timedelta(days=1) if latest < dt.date.max else latest)
    ]
    span = f", first {min(times)}, latest {max(times)}" if times else ""
    head = f"holdout fence: {count} exposure(s){span}{tail}"
    if spoiled:
        print(
            f"{head} (see {EXPOSURE_LOG}); possibly spoiled: {', '.join(spoiled)}: tell the owner",
            file=out,
        )
    elif not scenarios:
        print(f"{head}; no scenarios to judge", file=out)
    else:
        print(f"{head}; every scenario was written after the latest exposure", file=out)


def cmd_at_path(root: Path, holdout: Path, ids: list[str], out=None) -> int:
    out = out or sys.stdout
    scenarios = load(holdout)
    if scenarios is None:
        print("no holdout on this install", file=out)
        return 0
    if holdout.resolve() == DEFAULT_HOLDOUT.resolve():
        _ensure_ignore(holdout.parent)
    chosen = [sc for sc in scenarios if not ids or sc.sid in ids]
    missing = sorted(set(ids) - {sc.sid for sc in scenarios})
    for sid in missing:
        print(f"{sid} UNKNOWN", file=out)
    for sc in chosen:
        if sc.problem:
            print(f"{sc.sid} MALFORMED — {sc.problem}", file=out)
            continue
        arm = run_scenario(sc, root)
        if arm.passed:
            print(f"{sc.sid} PASS", file=out)
        else:
            print(f"{sc.sid} FAIL — observed {describe_failed_arm(arm)}", file=out)
    return 2 if missing else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pr", type=int, help="judge this merged PR")
    mode.add_argument("--at-path", type=Path, help="run scenarios against this directory")
    mode.add_argument(
        "--has-holdout",
        action="store_true",
        help="print whether this install keeps a holdout, and where; reads nothing in it",
    )
    ap.add_argument("--repo", help="owner/name (default: the checkout's own repo)")
    ap.add_argument("--root", type=Path, help="git checkout to cut worktrees from (default: cwd)")
    ap.add_argument("--holdout", type=Path, default=DEFAULT_HOLDOUT)
    ap.add_argument(
        "--scenario", action="append", default=[], help="with --at-path: only these ids"
    )
    args = ap.parse_args(argv)
    if args.scenario and args.at_path is None:
        ap.error("--scenario applies to --at-path only")
    if args.has_holdout:
        if args.holdout.is_file():
            print(f"holdout: present at {args.holdout}")
        else:
            print(f"holdout: none (would be {args.holdout})")
        return 0
    try:
        if args.at_path is not None:
            return cmd_at_path(args.at_path.resolve(), args.holdout, args.scenario)
        root = (args.root or Path.cwd()).resolve()
        root = Path(_git(root, "rev-parse", "--show-toplevel").strip())
        return cmd_pr(args.pr, args.repo, root, args.holdout)
    except RunnerError as exc:
        print(f"holdout: cannot run: {exc}", file=sys.stdout)
        return 3
    except Exception as exc:  # noqa: BLE001
        # Exit 1 means an attributable failure, so nothing else may end with it.
        print(f"holdout: cannot run: {type(exc).__name__}: {exc}", file=sys.stdout)
        return 3


if __name__ == "__main__":
    sys.exit(main())
