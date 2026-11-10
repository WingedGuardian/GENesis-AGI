#!/usr/bin/env python3
"""Run an install's hidden holdout scenarios against a merged PR.

A holdout is a small set of exact behavioural checks that builders never see,
so a builder cannot make its own tests agree with its code instead of with the
intent. The scenarios live outside the repository, by default in
``~/.genesis/holdout/HOLDOUT.md``; ``docs/reference/holdout.md`` documents the
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
``fixes``, ``resolves``, ``implements`` or ``part of`` ``#N``. A linked intent
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
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_HOLDOUT = Path.home() / ".genesis" / "holdout" / "HOLDOUT.md"

# A holdout step is a deterministic, local check that normally finishes in
# well under a second. 120 s is two orders of magnitude above that; past it the
# step is hung, and a hung step must not hold a validator pass indefinitely.
STEP_TIMEOUT_S = 120

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
_FIELD = re.compile(r"^([A-Z][A-Za-z-]*):[ \t]*(.*)$")
_FENCE = re.compile(r"^(```+|~~~+)")
# The only inherited variables a step sees. Everything else, credentials and the
# SSH agent included, is dropped rather than filtered by name.
_INHERITED_ENV = ("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "USER", "LOGNAME", "SHELL")
_CLOSING_REF = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|implements?|part\s+of)[:\s]+#(\d+)\b"
)


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
    for line in text.splitlines():
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


def step_env(root: Path, home: Path, tmp: Path) -> dict[str, str]:
    """The environment a step runs in: the code under test, nothing live."""
    env = {k: os.environ[k] for k in _INHERITED_ENV if k in os.environ}
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
        with open(out_path, "wb") as out_file:
            proc = subprocess.Popen(
                ["bash", str(script)],
                cwd=str(root),
                env=step_env(root, home, tmp),
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
                _kill_group(proc)  # whatever the step left running
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


def touched(sc: Scenario, files: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(f, pat) for pat in sc.touches for f in files)


def _scratch_base() -> str:
    base = Path.home() / "tmp"
    return str(base) if base.is_dir() else tempfile.gettempdir()


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise RunnerError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def gh_json(args: list[str], cwd: Path) -> dict:
    """Run a read-only ``gh`` query in ``cwd`` and parse its JSON. Tests replace this."""
    proc = subprocess.run(["gh", *args], capture_output=True, text=True, check=False, cwd=str(cwd))
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
        ["repo", "view", *([repo] if repo else []), "--json", "defaultBranchRef"], root
    )
    default_name = (default.get("defaultBranchRef") or {}).get("name")
    if info.get("baseRefName") != default_name:
        raise RunnerError(
            f"PR #{pr} merged into {info.get('baseRefName')}, not the default branch "
            f"{default_name}: its first parent is not the state it was built against"
        )
    linked = {
        int(ref["number"])
        for ref in info.get("closingIssuesReferences") or []
        if isinstance(ref, dict) and str(ref.get("number", "")).isdigit()
    }
    linked |= {int(n) for n in _CLOSING_REF.findall(info.get("body") or "")}
    return info["mergeCommit"]["oid"], linked


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
    merge, linked = resolve_merge(pr, repo, root)
    try:
        _git(root, "cat-file", "-e", f"{merge}^{{commit}}")
    except RunnerError:
        _git(root, "fetch", "--quiet", "origin", merge)
    parent = _git(root, "rev-parse", f"{merge}^1")
    # --no-renames: a moved file lists both paths, so a scenario on the old
    # path still runs when its code is moved away.
    changed = _git(root, "diff", "--name-only", "--no-renames", parent, merge)
    files = [f for f in changed.splitlines() if f]
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


def cmd_at_path(root: Path, holdout: Path, ids: list[str], out=None) -> int:
    out = out or sys.stdout
    scenarios = load(holdout)
    if scenarios is None:
        print("no holdout on this install", file=out)
        return 0
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
    ap.add_argument("--repo", help="owner/name (default: the checkout's own repo)")
    ap.add_argument("--root", type=Path, help="git checkout to cut worktrees from (default: cwd)")
    ap.add_argument("--holdout", type=Path, default=DEFAULT_HOLDOUT)
    ap.add_argument(
        "--scenario", action="append", default=[], help="with --at-path: only these ids"
    )
    args = ap.parse_args(argv)
    if args.scenario and args.at_path is None:
        ap.error("--scenario applies to --at-path only")
    try:
        if args.at_path is not None:
            return cmd_at_path(args.at_path.resolve(), args.holdout, args.scenario)
        root = (args.root or Path.cwd()).resolve()
        root = Path(_git(root, "rev-parse", "--show-toplevel"))
        return cmd_pr(args.pr, args.repo, root, args.holdout)
    except RunnerError as exc:
        print(f"holdout: cannot run: {exc}", file=sys.stdout)
        return 3
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # Exit 1 means an attributable failure, so nothing else may end with it.
        print(f"holdout: cannot run: {type(exc).__name__}: {exc}", file=sys.stdout)
        return 3


if __name__ == "__main__":
    sys.exit(main())
