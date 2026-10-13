"""Whether ~/.genesis/update_state.json still guards merged code.

An update that merged code but never ran bootstrap on it (a refused or
conflicted ``update.sh --post-merge``, a crash) leaves update_state.json in
place. deploy_code_only.sh and the live engine read it and refuse to restart
onto that code. Anything that would delete or overwrite the file asks this
module first: the dashboard's progress poll and supervised start
(dashboard/routes/updates.py), and update.sh before a new run writes its own
state. One rule, so the shell and the dashboard cannot drift (#3139, #3138).

The rule: only a state whose phase is "done", or a "fetching" state whose
rollback tag still resolves to HEAD (git says nothing merged), is safe to
replace. A "merging" state whose tag is still HEAD guards too, but for a
different reason: it is what a merge-conflict exit leaves (update.sh aborts the
merge and keeps the state as the resolver's anchor for --post-merge), so the
command line reports it separately and update.sh names the right remedy. A missing file guards nothing. Every other state guards, including a
file that cannot be read or parsed, one that is not a JSON object, and one that
names no phase: doubt keeps the guard. Retiring it on purpose is the job of that
run's ``--post-merge``, bootstrap's crash recovery, or the dashboard's Dismiss.

Command line (``python3 -I -S``, stdlib only)::

    update_state_guard.py <state-file> <repo>

Exit 0 when the file guards merged code, 1 when it does not (or is absent),
3 when it guards an update that stopped on merge conflicts with nothing merged.
Any other exit means the check itself failed; callers treat that as guarding.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

GUARDS = 0
SAFE = 1
CONFLICT_PENDING = 3


def _rev(repo: str, rev: str) -> str:
    # Local reads only; GIT_* from the caller would point git elsewhere.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "-q", rev],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    ).stdout.strip()


def state_guards(state: object, repo: str) -> bool:
    """The rule above, for an already-parsed state (``None``: no file)."""
    if state is None:
        return False
    if not isinstance(state, dict):
        return True
    phase = state.get("phase")
    if phase == "done":
        return False
    if phase != "fetching":
        return True
    return not _tag_is_head(state, repo)


def _tag_is_head(state: dict, repo: str) -> bool:
    tag = str(state.get("rollback_tag") or "")
    if not tag or tag.startswith("-"):
        return False
    try:
        tagged, head = _rev(repo, f"{tag}^{{commit}}"), _rev(repo, "HEAD")
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(tagged) and tagged == head


def file_guards(path: str, repo: str) -> bool:
    """The rule for the file on disk: unreadable or unparseable guards."""
    if not os.path.lexists(path):
        return False
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return True
    return state_guards(state, repo)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: update_state_guard.py <state-file> <repo>", file=sys.stderr)
        return 2
    path, repo = argv
    if not file_guards(path, repo):
        return SAFE
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return GUARDS
    if isinstance(state, dict) and state.get("phase") == "merging" and _tag_is_head(state, repo):
        return CONFLICT_PENDING
    return GUARDS


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
