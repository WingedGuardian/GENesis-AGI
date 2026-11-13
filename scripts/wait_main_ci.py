#!/usr/bin/env python3
"""Wait until main's CI has a verdict on a commit you merged. A CLI, not a hook.

    python3 scripts/wait_main_ci.py <merge-sha>              # 4h ceiling
    python3 scripts/wait_main_ci.py <merge-sha> --wait=7200  # or an explicit bound

Run it from a checkout of the repository. Exits 0 when a run containing the
commit concluded ``success``; 1 when one concluded red (``failure``,
``timed_out``, ``startup_failure``), with the failing jobs printed; 2 when main's
CI WAS read and no run containing the commit has concluded within the ceiling;
3 when the commit cannot be checked (unknown here, or not on ``origin/main``:
pass the merge commit, not the PR's head) or main's CI state could not be
established by the deadline (the last read failed, or the search ran out of
pages before reaching a run older than the commit). Exit 2 is the closing-session
skill's trigger for batching merges, so it is never returned on a read this
script could not complete.

WHICH RUN COUNTS. main's ``ci.yml`` push runs share one concurrency group that
never cancels the run in progress, but a newer push replaces the run still
PENDING (``ci.yml``, ``concurrency``). Measured 2026-11-13: 17 of main's last 30
push runs were cancelled while several sessions merged. So a merge's own run
often never runs. A cancelled run, or one that concluded without a signal
(skipped, neutral, stale, action_required), says nothing. Any run whose head
DESCENDS from the commit runs the whole-tree jobs (lint, the test suite) on it,
so the verdict is the newest containing run that concluded WITH a signal; newer
containing runs still queued are not waited for. This is the rule the merge
gate's main-CI note uses (``_main_ci_red_block`` in ``scripts/hooks/
git_push_guard.py``), with the same conclusion sets, pinned by a test.

NOT COVERED: jobs that diff a push against its ``before`` commit (leak-detector,
migration-check, changelog-fragments, collected-floor) check only the newest
push's own diff, so a merge whose run was cancelled is never diff-checked on
main by them. That gap is in CI, not here; a later run's green says nothing
about it.

The closing-session skill's merge pacing (one merge per concluded main run)
names this script.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping

DEFAULT_WAIT_S = 4 * 3600
POLL_S = 60
WORKFLOW = "ci.yml"
RUN_LIMIT = 30
#: Widening windows searched for a containing verdict, newest first. A window
#: whose oldest run still contains the commit may be hiding the deciding run.
RUN_WINDOWS = (RUN_LIMIT, 100, 300, 1000)

#: Conclusions that say nothing about the commit; must equal git_push_guard's
#: _MAIN_CI_NO_SIGNAL (a test pins it).
NO_SIGNAL = frozenset({"cancelled", "skipped", "neutral", "stale", "action_required", ""})
#: Conclusions that mean red; must equal git_push_guard's _MAIN_CI_RED.
RED = frozenset({"failure", "timed_out", "startup_failure"})


class Unknown(Exception):
    """The ancestry of a run head could not be read: neither yes nor no."""


def decide(
    runs: Iterable[Mapping[str, str]], contains: Callable[[str], bool]
) -> tuple[str, Mapping[str, str] | None]:
    """The verdict on the commit from runs newest first: ``("verdict", run)`` for
    the newest run that contains it and concluded with a signal, else
    ``("wait", None)``. Runs that predate the commit never count. A signal is
    ``success`` or a RED conclusion; any other value (NO_SIGNAL, or one GitHub
    adds later) is passed over, never read as either."""
    for run in runs:
        if run.get("status") != "completed":
            continue
        if (run.get("conclusion") or "") not in RED | {"success"}:
            continue
        if contains(str(run.get("headSha") or "")):
            return "verdict", run
    return "wait", None


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, timeout=120)


def _gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=True, timeout=60
    ).stdout


def _run_list_args(repo: str | None, limit: int = RUN_LIMIT) -> list[str]:
    args = [
        "run",
        "list",
        "--workflow",
        WORKFLOW,
        "--branch",
        "main",
        "--event",
        "push",
        "--limit",
        str(limit),
        "--json",
        "databaseId,headSha,status,conclusion",
    ]
    return args + (["--repo", repo] if repo else [])


def _runs(repo: str | None, limit: int = RUN_LIMIT) -> list[dict]:
    return json.loads(_gh(*_run_list_args(repo, limit)))


def search(
    repo: str | None, contains: Callable[[str], bool]
) -> tuple[str, Mapping[str, str] | None, bool]:
    """``decide`` over widening windows of main's push runs.

    Returns ``(state, run, complete)``. ``complete`` is True when the search saw
    every run that could hold the verdict: the window reached a run that does
    NOT contain the commit (older runs cannot either), or held fewer runs than
    asked for (the end of history). A window that is full and still contains
    the commit at its oldest row is widened; if the widest one is too, the
    search is incomplete and its "no verdict" proves nothing."""
    for limit in RUN_WINDOWS:
        runs = _runs(repo, limit)
        state, run = decide(runs, contains)
        if state == "verdict":
            return state, run, True
        if len(runs) < limit:
            return "wait", None, True
        oldest = str(runs[-1].get("headSha") or "")
        # Only a KNOWN commit that predates the merge proves nothing older can
        # contain it; a row with no commit id proves nothing, so keep widening.
        if oldest and not contains(oldest):
            return "wait", None, True
    return "wait", None, False


def _contains(sha: str) -> Callable[[str], bool]:
    """Whether a run head descends from ``sha``: git exit 0 yes, 1 no, anything
    else (an object git does not know) is Unknown, never a quiet no."""

    def check(head: str) -> bool:
        if not head:
            return False
        result = _git("merge-base", "--is-ancestor", sha, head)
        if result.returncode in (0, 1):
            return result.returncode == 0
        raise Unknown(result.stderr.strip() or f"git exit {result.returncode}")

    return check


def _resolve(sha: str) -> str:
    """The full commit id, after checking it is on origin/main. Raises Unknown."""
    fetched = _git("fetch", "-q", "origin", "main")
    if fetched.returncode != 0:
        raise Unknown("git fetch origin main failed: " + fetched.stderr.strip())
    parsed = _git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
    if parsed.returncode != 0:
        raise Unknown(f"{sha} is not a commit this checkout knows")
    full = parsed.stdout.strip()
    on_main = _git("merge-base", "--is-ancestor", full, "origin/main")
    if on_main.returncode != 0:
        raise Unknown(f"{sha} is not on origin/main (pass the merge commit, not the PR head)")
    return full


def _failed_jobs(run_id: str, repo: str | None) -> list[str]:
    args = ["run", "view", run_id, "--json", "jobs"] + (["--repo", repo] if repo else [])
    jobs = json.loads(_gh(*args)).get("jobs") or []
    return [j["name"] for j in jobs if j.get("conclusion") not in ("success", "skipped")]


def main(
    argv: list[str] | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("sha", help="the merge commit on main")
    parser.add_argument("--wait", type=int, default=DEFAULT_WAIT_S, help="ceiling in seconds")
    parser.add_argument("--repo", default=None, help="owner/name; default: gh's current repo")
    args = parser.parse_args(argv)

    try:
        sha = _resolve(args.sha)
    except (Unknown, subprocess.SubprocessError, OSError) as exc:
        print(f"wait_main_ci: cannot check {args.sha}: {exc}", file=sys.stderr)
        return 3
    contains = _contains(sha)
    deadline = clock() + args.wait
    while True:
        try:
            _git("fetch", "-q", "origin", "main")
            state, run, observed = search(args.repo, contains)
            if not observed:
                print("wait_main_ci: the widest run window still ends inside runs that "
                      "contain the commit; retrying", file=sys.stderr)
        except (Unknown, subprocess.SubprocessError, OSError, ValueError) as exc:
            state, run, observed = "wait", None, False
            print(f"wait_main_ci: could not read main's runs ({exc}); retrying", file=sys.stderr)
        if state == "verdict" and run is not None:
            conclusion = run.get("conclusion")
            print(
                f"run {run.get('databaseId')}: main at {str(run.get('headSha'))[:12]}, "
                f"which includes {sha[:12]}, is {conclusion}"
                + (
                    ""
                    if run.get("headSha") == sha
                    else " (a later push's run; a red may not be this merge's)"
                )
            )
            if conclusion == "success":
                return 0
            try:
                failed = _failed_jobs(str(run.get("databaseId")), args.repo)
                print("failed jobs: " + (", ".join(failed) or "(none listed)"))
            except (subprocess.SubprocessError, OSError, ValueError):
                print("failed jobs: (could not be read)")
            return 1
        if clock() >= deadline:
            if not observed:
                print(
                    f"wait_main_ci: main's CI state could not be established for "
                    f"{sha[:12]} within {args.wait}s (see the read errors above)",
                    file=sys.stderr,
                )
                return 3
            print(
                f"wait_main_ci: no run containing {sha[:12]} concluded with a signal "
                f"within {args.wait}s",
                file=sys.stderr,
            )
            return 2
        sleep(POLL_S)


if __name__ == "__main__":
    sys.exit(main())
