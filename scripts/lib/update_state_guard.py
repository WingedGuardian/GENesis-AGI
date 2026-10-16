"""Whether ~/.genesis/update_state.json still guards merged code (#3139).

update.sh writes update_state.json at each phase. An update that merged code it
has not activated (a crash, a refused or interrupted ``--post-merge``) leaves it
behind, and so does a merge-conflict exit, whose resolver needs its rollback
tag. deploy_code_only.sh and the live engine refuse to restart or move the
checkout while such a file stands, and update.sh and the dashboard refuse to
overwrite or delete it. All of them ask this module, so the shell and the
dashboard cannot disagree.

Verdicts, in order:

1. no file: SAFE;
2. a file that cannot be read or parsed, or is not a JSON object: GUARDS;
3. phase "done": SAFE (written just before update.sh deletes the file);
4. phase "fetching": SAFE only when the rollback tag is HEAD, the state names
   the commit it fetched (``deploy_head``), and it says a plain run wrote it
   (``post_merge`` exactly false). A ``--post-merge`` run writes "fetching" over
   code that is already merged, sometimes with a fallback tag made at that
   merged HEAD, and a state without the key (older, or hand-edited) cannot rule
   that out; else GUARDS;
5. phase "merging" with the rollback tag at HEAD, no MERGE_HEAD, and no merge
   residue (``genesis_merge_residue`` in deploy_checkout.sh, the check
   bootstrap's crash recovery runs, so the two agree; it counts every tracked
   change, ephemeral paths included): CONFLICT_PENDING when
   update_conflicts.json names the same ``deploy_head`` (update.sh writes it
   only for a real conflict), otherwise SAFE (a run killed before its merge);
6. every other "merging", any other phase, or none: GUARDS.

The pid is not an input: callers answer liveness first (update.lock in the
shell, env.update_in_progress() in the dashboard). Anything this module cannot
decide GUARDS. Retiring a guarding file on purpose is the job of that update's
``--post-merge``, bootstrap's crash recovery, or the dashboard's Dismiss.

Command line (``python3 -I -S``, standard library only)::

    update_state_guard.py <state-file> <repo> [<conflicts-file>]

Exit 10 when the file guards nothing, 0 when it guards, 3 when it is a pending
merge conflict; the remedy is printed on stdout. Any failure inside the check
(git or bash not runnable, a state too deeply nested to parse) exits 0, guarding;
any other exit means the check could not run, and callers treat that as guarding
too. Only exit 10 lets a caller go on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

SAFE = "safe"
GUARDS = "guards"
CONFLICT_PENDING = "conflict_pending"

# SAFE is 10, a code no default failure produces: Python exits 1 on an uncaught
# exception and 2 on a usage error, a shell 126/127 when it cannot run the file,
# so none of those can ever read as "nothing to guard".
EXIT_CODES = {GUARDS: 0, SAFE: 10, CONFLICT_PENDING: 3}

_LIB = os.path.dirname(os.path.abspath(__file__))


# The dashboard runs this while it holds update.lock in a request thread, so a
# hung git (a wedged filesystem, a frozen cgroup) must not hold the lock for good.
# 120 s matches the dashboard's other checks; TimeoutExpired reaches every
# caller's exception path, which keeps the state (guards).
_TIMEOUT_S = 120


def _env() -> dict[str, str]:
    # Local reads only; GIT_* from the caller would point git elsewhere.
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _rev(repo: str, rev: str) -> str:
    return subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "-q", rev],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    ).stdout.strip()


def _tag_commit_is_head(state: dict, repo: str) -> str | None:
    """The rollback tag's commit when it is HEAD, else None."""
    tag = state.get("rollback_tag")
    if not isinstance(tag, str) or not tag or tag.startswith("-"):
        return None
    tagged = _rev(repo, f"refs/tags/{tag}^{{commit}}")
    head = _rev(repo, "HEAD^{commit}")
    return tagged if tagged and tagged == head else None


def _merge_residue(repo: str, rb_commit: str, deploy_head: str) -> str:
    """genesis_merge_residue's lines; any failure to run it is residue."""
    proc = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1/deploy_checkout.sh" && genesis_merge_residue "$2" "$3" "$4"',
            "update_state_guard",
            _LIB,
            repo,
            rb_commit,
            deploy_head,
        ],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=_TIMEOUT_S,
    )
    if proc.returncode != 0:
        return f"(the merge residue check failed: exit {proc.returncode})"
    return proc.stdout.strip()


def _conflict_recorded(conflicts_path: str, deploy_head: str) -> bool:
    """Does the conflict record name this commit? A record that exists but
    cannot be read, parsed, or is not an object counts as naming it: doubt keeps
    the conflict the resolver is waiting on."""
    if not os.path.lexists(conflicts_path):
        return False
    try:
        with open(conflicts_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return True
    if not isinstance(data, dict):
        return True
    return data.get("deploy_head") == deploy_head


def state_verdict(state: object, repo: str, conflicts_path: str | None = None) -> tuple[str, str]:
    """The rule for an already-parsed state. Returns (verdict, reason)."""
    if not isinstance(state, dict):
        return GUARDS, "it cannot be read"
    phase = state.get("phase")
    if phase == "done":
        return SAFE, "it finished"
    deploy_head = state.get("deploy_head")
    deploy_head = deploy_head if isinstance(deploy_head, str) else ""
    if phase == "fetching":
        # Only a state a plain run wrote (post_merge exactly False) can be safe: a
        # --post-merge, a state from before the key existed, or a hand edit may
        # be "fetching" over merged code at a fallback tag made at that HEAD.
        if state.get("post_merge") is not False:
            return GUARDS, "it may be a --post-merge run that stopped before bootstrap"
        if deploy_head and _tag_commit_is_head(state, repo):
            return SAFE, "it stopped before merging anything"
        return GUARDS, "phase 'fetching', but HEAD is not its rollback tag"
    if phase == "merging":
        rb_commit = _tag_commit_is_head(state, repo)
        if not rb_commit:
            return GUARDS, "it merged code bootstrap has not run on"
        if _rev(repo, "MERGE_HEAD"):
            return GUARDS, "a merge is in progress in the checkout"
        if _merge_residue(repo, rb_commit, deploy_head):
            return GUARDS, "tracked files changed while it was merging"
        if deploy_head and conflicts_path and _conflict_recorded(conflicts_path, deploy_head):
            return CONFLICT_PENDING, "it stopped on merge conflicts; nothing was merged"
        return SAFE, "it stopped before merging anything"
    if isinstance(phase, str) and phase:
        return GUARDS, f"it stopped in phase '{phase}'"
    return GUARDS, "it names no phase"


def verdict(state_path: str, repo: str, conflicts_path: str | None = None) -> tuple[str, str]:
    """The rule for the file on disk."""
    if not os.path.lexists(state_path):
        return SAFE, "there is no state file"
    try:
        with open(state_path, encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return GUARDS, "it cannot be read"
    if conflicts_path is None:
        conflicts_path = os.path.join(
            os.path.dirname(os.path.abspath(state_path)), "update_conflicts.json"
        )
    return state_verdict(state, repo, conflicts_path)


def message(state_path: str, verdict_: str, reason: str) -> str:
    """What every refusing caller says, so the remedy reads the same everywhere."""
    if verdict_ == CONFLICT_PENDING:
        return (
            f"{state_path} records an earlier update that stopped on merge conflicts; "
            "nothing was merged. Resolve them (Resolve with Opus on the dashboard's "
            "update card, or merge the deploy_head in update_conflicts.json and run "
            "scripts/update.sh --post-merge), or Dismiss it on the dashboard's update card."
        )
    return (
        f"{state_path} records an earlier update that has not finished ({reason}). "
        "Finish it with scripts/update.sh --post-merge, or Dismiss it on the "
        "dashboard's update card."
    )


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(
            "usage: update_state_guard.py <state-file> <repo> [<conflicts-file>]", file=sys.stderr
        )
        return 2
    state_path, repo = argv[0], argv[1]
    try:
        v, reason = verdict(state_path, repo, argv[2] if len(argv) == 3 else None)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001 - any failure to decide guards
        print(message(state_path, GUARDS, f"it could not be checked: {type(exc).__name__}"))
        return EXIT_CODES[GUARDS]
    if v != SAFE:
        print(message(state_path, v, reason))
    return EXIT_CODES[v]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
