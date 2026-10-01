"""deploy_candidates_plan.py — building `live` off the working tree, and moving
the checkout to it.

``build_plan`` merges each candidate's PINNED head (the commit `add` saw,
never the branch's current tip) onto a base with
``git merge-tree --write-tree`` and chains ``git commit-tree`` merges, writing
only objects until a ref points at them. Whether a candidate may go live at all
is decided before this, by deploy_candidates_gate.py; this module decides only
what merges, what conflicts, and what must go out with an excluded candidate.

Flat sibling of deploy_candidates.py (see that file for the commands).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Imported only through deploy_candidates.py, whose finder resolves the
# sibling modules from this directory: scripts/ is never on sys.path.
from deploy_candidates_core import (  # noqa: E402
    BASE_BRANCH,
    BASE_REMOTE,
    CANDIDATE_KEY,
    IDENTITY,
    LIVE_BRANCH,
    LIVE_REF,
    SYNC_HOOKS_TIMEOUT,
    TRAILER_KEY,
    Plan,
    Refusal,
    Repo,
    out,
)


def carries(mine: set[str], theirs: set[str], others: set[str]) -> bool:
    """Does a branch whose unmerged commits are ``mine`` carry the code of an
    excluded one whose unmerged commits are ``theirs``? ``others`` are the
    commits that belong to some third candidate both are stacked on.

    It does when they share a commit that ``others`` does not account for,
    UNLESS every commit of ``mine`` is in ``theirs``: then the excluded branch
    is stacked ON this one, and its commits are this one's own.

    Git cannot say which of two branches OWNS a commit both hold. A branch cut
    from an early commit of another, and a branch the other was cut from that
    then gained a commit, are the same graph; both read as carrying it, and the
    rule excludes (the safe side, reported by name)."""
    return bool((mine & theirs) - others) and not mine <= theirs


def build_plan(
    repo: Repo,
    base: str,
    cands: list[tuple[str, str]],
    rebuild_id: str,
    sticky: dict[str, str] | None = None,
    removed: dict[str, str] | None = None,
) -> Plan:
    """Merge each (branch, head) onto ``base`` in order. ``sticky`` maps a
    candidate to why it is excluded before any merge (missing branch, failed
    admission or gate). ``removed`` maps a branch that is NOT a candidate any
    more (a drop) to its head: its code must not stay live through another
    candidate either. Raises Refusal on a merge git cannot even attempt.

    DERIVATION. A candidate carrying an excluded candidate's unmerged commits
    carries its code, and is excluded with it. It is derived only from a ROOT
    exclusion (a sticky one, a conflict, or a removed branch), never from
    another derived one, so two candidates cannot exclude each other. The
    commits a third candidate accounts for (``others``) come only from
    candidates still going live whose head is an ANCESTOR of the excluded
    candidate's head: what both siblings share because both are stacked on a
    live third candidate is that candidate's code.

    A conflict is recomputed on every pass (a candidate that conflicted only
    with one excluded later can merge after all), and a derived exclusion whose
    source stops being excluded is released once; after that it stays, so the
    loop ends."""
    sticky = dict(sticky or {})
    removed = dict(removed or {})
    heads = dict(cands)
    all_heads = {**removed, **heads}
    # A head whose object is gone (a deleted, garbage-collected branch) has no
    # commits to compare; it is already excluded as a missing branch.
    commits = {
        b: set(repo.rev_list(h, "--not", base)) for b, h in all_heads.items() if repo.resolve(h)
    }
    anc: dict[tuple[str, str], bool] = {}

    def is_anc(x: str, e: str) -> bool:
        key = (all_heads[x], all_heads[e])
        if key not in anc:
            anc[key] = repo.is_ancestor(*key)
        return anc[key]

    derived: dict[str, tuple[str, str]] = {}  # branch -> (source, reason)
    released: set[str] = set()
    while True:
        excluded = dict(sticky)
        excluded.update({b: r for b, (_, r) in derived.items()})
        tip = base
        merged: list[tuple[str, str]] = []
        contained: list[str] = []
        conflicts: set[str] = set()
        for b, head in cands:
            if b in excluded:
                continue
            # Nothing to merge: the head is already in the tip (a branch at
            # origin/main, or one an earlier candidate contains). A merge
            # commit with that head as second parent collapses to ONE parent
            # when head == tip, which later rebuilds would read as foreign.
            if repo.is_ancestor(head, tip):
                contained.append(b)
                continue
            p = repo.git(
                "merge-tree", "--write-tree", "--name-only", "--no-messages", tip, head, check=False
            )
            if p.returncode == 1:
                files = [ln for ln in p.stdout.splitlines()[1:] if ln.strip()]
                excluded[b] = (
                    "conflicts with origin/main or an earlier candidate"
                    + (f" in {', '.join(files)}" if files else "")
                    + "; merge origin/main into it"
                )
                conflicts.add(b)
                continue
            if p.returncode != 0:
                raise Refusal(
                    f"git merge-tree could not merge {b} ({head[:12]}): {p.stderr.strip()} — nothing changed."
                )
            tree = p.stdout.splitlines()[0].strip()
            msg = (
                f"Deploy rebuild {rebuild_id}: merge {b} ({head[:12]})\n\n"
                f"{TRAILER_KEY}: {rebuild_id}\n{CANDIDATE_KEY}: {b}\n"
            )
            tip = repo.git(
                "commit-tree", tree, "-p", tip, "-p", head, input=msg, extra_env=IDENTITY
            ).stdout.strip()
            merged.append((b, head))
        roots = [e for e in (*sticky, *conflicts, *removed) if e in commits]
        going_live = [m for m, _ in merged] + contained
        changed = False
        for b, (src, _) in list(derived.items()):
            if src not in roots and b not in released:
                del derived[b]
                released.add(b)
                changed = True
        for b in going_live:
            for e in roots:
                if e == b:
                    continue
                others = set().union(
                    *(commits[x] for x in going_live if x not in (b, e) and is_anc(x, e))
                )
                if carries(commits[b], commits[e], others):
                    verb = "dropped" if e in removed else "excluded"
                    derived[b] = (e, f"derived from {verb} {e} (it carries {e}'s unmerged commits)")
                    changed = True
                    break
        if not changed:
            return Plan(base=base, tip=tip, merged=merged, excluded=excluded, contained=contained)


def stacked_on(repo: Repo, base: str, dropped_head: str, others: dict[str, str]) -> list[str]:
    """Which of ``others`` (branch -> head) carry ``dropped_head``'s unmerged
    commits, by the same rule build_plan derives exclusions with."""
    plan_heads = {"\0dropped": dropped_head, **others}
    commits = {b: set(repo.rev_list(h, "--not", base)) for b, h in plan_heads.items()}
    theirs = commits["\0dropped"]
    result = []
    for b in others:
        acc = set().union(
            *(commits[x] for x in others if x != b and repo.is_ancestor(others[x], dropped_head))
        )
        if carries(commits[b], theirs, acc):
            result.append(b)
    return result


def move_blockers(repo: Repo, tip: str) -> list[str]:
    """Paths the move to ``tip`` would have to overwrite on disk: files ``tip``
    adds that already exist untracked or ignored (or a file where it needs a
    directory). `git switch --no-overwrite-ignore` refuses on these; checking
    first lets a command refuse BEFORE it writes the manifest."""
    head = repo.resolve("HEAD")
    if not head:
        return []
    text = repo.git(
        "diff", "--no-renames", "--name-only", "--diff-filter=A", "-z", head, tip
    ).stdout
    blockers = []
    for path in [p for p in text.split("\0") if p]:
        full = repo.root / path
        if full.exists() or full.is_symlink():
            blockers.append(path)
            continue
        parent = Path(path).parent
        while str(parent) not in ("", "."):
            pf = repo.root / parent
            if pf.is_symlink() or (pf.exists() and not pf.is_dir()):
                blockers.append(f"{path} (a file is in the way at {parent})")
                break
            parent = parent.parent
    return blockers


def ensure_reflog_kept(repo: Repo) -> None:
    for key in (
        "gc.refs/heads/live.reflogExpire",
        "gc.refs/heads/live.reflogExpireUnreachable",
    ):
        if repo.git("config", "--get", key, check=False).stdout.strip() != "never":
            repo.git("config", key, "never")
    cur = repo.git("config", "--get", "core.logAllRefUpdates", check=False).stdout.strip().lower()
    if cur in ("false", "no", "off", "0"):
        repo.git("config", "core.logAllRefUpdates", "true")
        out("  NOTE: core.logAllRefUpdates was off; turned on so `live` keeps a reflog.")


def move_checkout(repo: Repo, plan: Plan, branch: str | None) -> bool:
    """Point the checkout at ``plan.tip``: not at all when nothing changed, the
    ref alone when only commits changed, otherwise ONE `git switch`. Returns
    whether the checkout's files changed. Git configuration for `live` is
    written only after the move succeeded, so a refused move writes nothing."""
    live_base, live_merged = repo.live_chain(plan.base)
    cur_tip = repo.resolve(LIVE_REF)
    files_moved = False
    if branch == LIVE_BRANCH and cur_tip and live_base == plan.base and live_merged == plan.merged:
        out(f"  checkout: unchanged ({cur_tip[:12]}): same origin/main, same candidate heads.")
    elif branch == LIVE_BRANCH and cur_tip and repo.tree(cur_tip) == repo.tree(plan.tip):
        # Same files under new commits: move the ref, not the working tree.
        repo.git(
            "update-ref",
            "-m",
            f"deploy-candidates: rebuild (tree unchanged) onto {plan.base[:12]}",
            LIVE_REF,
            plan.tip,
            cur_tip,
        )
        out(
            f"  checkout: tree unchanged; `live` moved to {plan.tip[:12]} without touching the files."
        )
    else:
        # ONE checkout. --no-overwrite-ignore: git otherwise overwrites an
        # ignored file in the way without asking (MEASURED, git 2.43); with it,
        # as with an untracked one, git refuses and changes nothing.
        p = repo.git("switch", "--no-overwrite-ignore", "-C", LIVE_BRANCH, plan.tip, check=False)
        if p.returncode != 0:
            raise Refusal(
                f"git refused to move the checkout to {plan.tip[:12]}; nothing moved:\n{p.stderr.strip()}"
            )
        out(f"  checkout: moved to {plan.tip[:12]}.")
        files_moved = True
    ensure_reflog_kept(repo)
    up = repo.git(
        "branch", "--set-upstream-to", f"{BASE_REMOTE}/{BASE_BRANCH}", LIVE_BRANCH, check=False
    )
    if up.returncode != 0:
        out(
            f"  WARNING: could not set `live` to track {BASE_REMOTE}/{BASE_BRANCH}: {up.stderr.strip()}"
        )
    return files_moved


def sync_git_hooks(repo: Repo) -> None:
    """Install the git hooks from the checkout (deploy_code_only.sh's step):
    sync-hooks.sh is idempotent and never overwrites a hook someone modified;
    its non-zero exits are reported, never fatal. On `live` the hook sources
    are reviewed main's, since admission refuses a candidate that changes them."""
    script = repo.root / "scripts" / "hooks" / "sync-hooks.sh"
    if not script.is_file():
        out(f"  NOTE: {script} is missing; the git hook copies were not synced.")
        return
    try:
        p = subprocess.run(
            ["bash", str(script), "--quiet"],
            cwd=str(repo.root),
            env=repo.env,
            capture_output=True,
            text=True,
            timeout=SYNC_HOOKS_TIMEOUT,
        )
        rc = p.returncode
    except (OSError, subprocess.TimeoutExpired) as exc:
        out(f"  NOTE: sync-hooks.sh could not run ({exc}); the git hooks are as they were.")
        return
    if rc == 0:
        out("  Git hook copies in sync.")
    elif rc == 2:
        out("  NOTE: sync-hooks.sh left a user-modified git hook alone (exit 2).")
    else:
        out(
            f"  NOTE: sync-hooks.sh could not sync the git hooks (exit {rc}); they are as they were."
        )


def fast_forward_main(repo: Repo, base: str) -> None:
    """Advance local main to origin/main by fetching into it: git refuses a
    branch checked out in another worktree and anything but a fast-forward
    (MEASURED, git 2.43), where `update-ref` would move a checked-out branch
    under that worktree."""
    if repo.resolve(f"refs/heads/{BASE_BRANCH}") == base:
        out(f"  main: already at {base[:12]}.")
        return
    p = repo.git("fetch", "-q", ".", f"{base}:refs/heads/{BASE_BRANCH}", check=False)
    if p.returncode == 0:
        out(f"  main: fast-forwarded to {base[:12]}.")
    else:
        out(
            f"  WARNING: local main not fast-forwarded ({p.stderr.strip() or 'exit ' + str(p.returncode)})."
        )
