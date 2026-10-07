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

import os
import subprocess
from pathlib import Path
from typing import NamedTuple

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
    after_move,
    out,
)
from deploy_candidates_gate import SYNC_HOOKS, is_symlink_at, sync_hook_names  # noqa: E402


def shared_with(repo: Repo, base: str, heads: dict[str, str]) -> dict[str, list[str]]:
    """For each branch, the other branches it shares an UNMERGED commit with
    (one reachable from both heads and not from ``base``), in ``heads`` order.

    Two candidates may not share one. Git cannot say which of two branches owns
    a commit both hold, so with two listed there is no answer to "is this code
    live?" once one of them is excluded, dropped or retired: a stack goes in as
    its TOP branch, one candidate carrying all of its commits. A head whose
    object is gone has no commits to compare (it is excluded as missing)."""
    commits = {b: set(repo.rev_list(h, "--not", base)) for b, h in heads.items() if repo.resolve(h)}
    names = list(commits)
    result: dict[str, list[str]] = {}
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            if commits[a] & commits[b]:
                result.setdefault(a, []).append(b)
                result.setdefault(b, []).append(a)
    return result


def build_plan(
    repo: Repo,
    base: str,
    cands: list[tuple[str, str]],
    rebuild_id: str,
    sticky: dict[str, str] | None = None,
) -> Plan:
    """Merge each (branch, head) onto ``base`` in order. ``sticky`` maps a
    candidate to why it is excluded before any merge (missing branch, failed
    admission or gate). Raises Refusal on a merge git cannot even attempt.

    Candidates that share an unmerged commit are ALL excluded, by name (see
    shared_with): `add` refuses that, so it arises only from a hand-edited
    manifest or a base that moved backwards, and no rule can say whose code
    the shared commits are. Nothing is derived from another exclusion: a
    candidate carrying no other candidate's commits carries no other
    candidate's code."""
    excluded = dict(sticky or {})
    shared = shared_with(repo, base, dict(cands))
    for b, _ in cands:
        if b in shared and b not in excluded:
            excluded[b] = (
                f"shares unmerged commits with {', '.join(shared[b])}: two candidates may not "
                "(a stack goes in as its top branch); drop all but one"
            )
    tip = base
    merged: list[tuple[str, str]] = []
    contained: list[str] = []
    for b, head in cands:
        if b in excluded:
            continue
        # Nothing to merge: the head is already in origin/main. (A head inside
        # an earlier candidate shares its commits and is excluded above.) A merge
        # commit with that head as second parent would collapse to ONE parent
        # when head == tip, which later rebuilds would read as foreign.
        if repo.is_ancestor(head, tip):
            contained.append(b)
            continue
        # git's global --attr-source: the in-tree merge rules come from the
        # reviewed base, never from the checkout's index, which on `live` holds
        # candidate code ($GIT_DIR/info/attributes and core.attributesFile, which
        # only the install's owner writes, still apply): a candidate's
        # `.gitattributes` (`merge=union`) would otherwise merge two conflicting
        # candidates into an unreviewed concatenation (MEASURED, git 2.43).
        p = repo.git(
            f"--attr-source={base}",
            "merge-tree",
            "--write-tree",
            "--name-only",
            "--no-messages",
            tip,
            head,
            check=False,
        )
        if p.returncode == 1:
            files = [ln for ln in p.stdout.splitlines()[1:] if ln.strip()]
            excluded[b] = (
                "conflicts with origin/main or an earlier candidate"
                + (f" in {', '.join(files)}" if files else "")
                + "; merge origin/main into it"
            )
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
    return Plan(base=base, tip=tip, merged=merged, excluded=excluded, contained=contained)


def move_blockers(repo: Repo, tip: str) -> list[str]:
    """Paths the move to ``tip`` would have to overwrite on disk: a path ``tip``
    adds where an UNTRACKED or IGNORED file already is (at it, or under it when
    it is a directory now), or an untracked file where ``tip`` needs a
    directory, or a tracked path with uncommitted changes that ``tip`` changes
    (the dirty check excuses machine-written ones such as AGENTS.md, and git
    refuses to overwrite them). `git switch --no-overwrite-ignore` refuses on
    all of these; checking first lets a command refuse BEFORE it writes the
    manifest. Clean tracked content in the way is git's to replace (a tracked
    directory that becomes a file, or the reverse: MEASURED, git 2.43 `switch`
    does both), so it is not a blocker."""
    head = repo.resolve("HEAD")
    if not head:
        return []
    blockers = []
    changed = set(
        p
        for p in repo.git("diff", "--no-renames", "--name-only", "-z", head, tip).stdout.split("\0")
        if p
    )
    status = repo.git("status", "--porcelain", "--no-renames", "-z").stdout
    for rec in [r for r in status.split("\0") if r]:
        if not rec.startswith(("??", "!!")) and rec[3:] in changed:
            blockers.append(
                f"{rec[3:]} (it has uncommitted changes, which the move would overwrite)"
            )
    text = repo.git(
        "diff", "--no-renames", "--name-only", "--diff-filter=A", "-z", head, tip
    ).stdout
    for path in [p for p in text.split("\0") if p]:
        full = repo.root / path
        found = _untracked_at(repo, path) if (full.exists() or full.is_symlink()) else []
        if found == [path]:
            blockers.append(path)
            continue
        if found:
            more = f", and {len(found) - 10} more" if len(found) > 10 else ""
            blockers.append(f"{path} (untracked content under it: {', '.join(found[:10])}{more})")
            continue
        parent = Path(path).parent
        while str(parent) not in ("", "."):
            pf = repo.root / parent
            if pf.is_symlink() or (pf.exists() and not pf.is_dir()):
                if repo.blob_at(head, str(parent)) is None:
                    blockers.append(f"{path} (an untracked file is in the way at {parent})")
                break
            parent = parent.parent
    return blockers


def _worktree_status(repo: Repo, paths: list[str] | None = None) -> set[str]:
    """Every path git would report under ``paths``, for comparing the working
    tree before and after a move: tracked changes, EACH untracked file (`-uall`,
    never a directory collapsed to one line — git writes a file inside an
    already-untracked directory and that must still show as changed), and
    ignored files (`--ignored` — a partial checkout can write one the move's
    smudge filter then chokes on). `-z` so a path with a newline is one record.
    A git failure raises (check defaults True) rather than reading as an empty
    tree, which would turn a partial move into 'nothing moved'.

    ``paths`` scopes the scan to the move's own files (the diff HEAD..tip): a
    partial checkout only touches those, so scoping there catches every partial
    write while NOT enumerating an ignored tree the move never touches (a
    repository's own `.venv` is tens of thousands of `--ignored` entries). An
    empty list would mean "all paths" to git, so it falls back to the whole
    tree — a move with no paths never reaches the switch anyway."""
    args = ["status", "--porcelain", "--no-renames", "-uall", "--ignored", "-z"]
    if paths:
        args += ["--", *paths]
    text = repo.git(*args).stdout
    return {r for r in text.split("\0") if r}


def _untracked_at(repo: Repo, path: str) -> list[str]:
    """The untracked or ignored files at ``path``, or under it (none: []).
    (`ls-files --others` without --exclude-standard lists ignored files too;
    :(literal) keeps a name from being read as a pattern.)"""
    p = repo.git("ls-files", "-z", "--others", "--", f":(literal){path}")
    return [f for f in p.stdout.split("\0") if f]


def ensure_reflog_kept(repo: Repo) -> None:
    # The live-ref pattern keeps `live`'s own reflog; the unpatterned keys keep
    # HEAD's, which is a different log (`$GIT_DIR/logs/HEAD`) and the one the
    # serving-commit reader reads (deploy_status.sh) to find the booted commit.
    # A `gc.<pattern>.reflogExpire` matches only refs under that pattern, so HEAD
    # would otherwise keep git's 90d/30d defaults and a long-lived server could
    # lose the boot entry, making readiness refuse. With the default at `never`
    # the reader's `gc.reflogExpireUnreachable` resolves to the 0 cutoff, which
    # it treats as "never expired" (MEASURED, git 2.43).
    for key in (
        "gc.refs/heads/live.reflogExpire",
        "gc.refs/heads/live.reflogExpireUnreachable",
        "gc.reflogExpire",
        "gc.reflogExpireUnreachable",
    ):
        if repo.git("config", "--get", key, check=False).stdout.strip() != "never":
            repo.git("config", key, "never")
    cur = repo.git("config", "--get", "core.logAllRefUpdates", check=False).stdout.strip().lower()
    if cur in ("false", "no", "off", "0"):
        repo.git("config", "core.logAllRefUpdates", "true")
        out("  NOTE: core.logAllRefUpdates was off; turned on so `live` keeps a reflog.")


class Move(NamedTuple):
    """What a move did: whether the checkout's files changed, and the commit
    `live` is at afterwards (its old tip when nothing changed, else the plan's
    tip). Every post-move WARNING names ``at``, never a commit that is not live."""

    files: bool
    at: str


def move_checkout(repo: Repo, plan: Plan, branch: str | None) -> Move:
    """Point the checkout at ``plan.tip``: not at all when nothing changed, the
    ref alone when only commits changed, otherwise ONE `git switch`. Returns
    whether the checkout's files changed and where `live` is (a Move). Git
    configuration for `live` is
    written only after the move succeeded, so a refused move writes nothing; a
    failure after the move is a WARNING (core.after_move), never a refusal."""
    live_base, live_merged = repo.live_chain(plan.base)
    cur_tip = repo.resolve(LIVE_REF)
    files_moved = False
    at = plan.tip
    if (
        branch == LIVE_BRANCH
        and cur_tip
        and live_base == plan.base
        and live_merged == plan.merged
        # The same merges can still give another tree (a repository-local merge
        # driver changed: MEASURED, git 2.43), so the tree decides, not the list.
        and repo.tree(cur_tip) == repo.tree(plan.tip)
    ):
        at = cur_tip  # nothing moves: `live` stays where it is
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
        # The files the switch will write are diff(HEAD, tip); a partial write
        # is within them, so the before/after comparison is scoped there (see
        # _worktree_status). HEAD unresolvable (unborn/detached) → whole tree.
        cur_head = repo.resolve("HEAD")
        move_paths = (
            [
                p
                for p in repo.git(
                    "diff", "--no-renames", "--name-only", "-z", cur_head, plan.tip
                ).stdout.split("\0")
                if p
            ]
            if cur_head
            else None
        )
        before = _worktree_status(repo, move_paths)
        p = repo.git("switch", "--no-overwrite-ignore", "-C", LIVE_BRANCH, plan.tip, check=False)
        if p.returncode != 0:
            # git returns a post-checkout hook's status AFTER the checkout has
            # moved (MEASURED, git 2.43): read where HEAD is, never trust rc.
            head_ref = repo.git("symbolic-ref", "-q", "HEAD", check=False).stdout.strip()
            moved = head_ref == LIVE_REF and repo.resolve("HEAD") == plan.tip
            if not moved:
                # HEAD unmoved is not "nothing changed": git can rewrite part of
                # the working tree and then fail (a required smudge filter, a
                # write error; MEASURED, git 2.43). Compare, never infer.
                changed = sorted({rec[3:] for rec in before ^ _worktree_status(repo, move_paths)})
                if changed:
                    more = f"\n  … and {len(changed) - 20} more" if len(changed) > 20 else ""
                    raise Refusal(
                        f"git failed partway through moving the checkout to {plan.tip[:12]}: HEAD "
                        f"is still {head_ref or 'detached'}, but these paths changed:\n"
                        + "\n".join("  " + c for c in changed[:20])
                        + more
                        + "\nThe working tree mixes two trees; nothing was restarted. Put the tracked "
                        "ones back (git restore --source=HEAD --staged --worktree -- <path>), delete "
                        "the new untracked ones, fix the cause below, then retry:\n"
                        + p.stderr.strip()
                    )
                raise Refusal(
                    f"git refused to move the checkout to {plan.tip[:12]}; nothing moved:\n{p.stderr.strip()}"
                )
            out(
                f"  WARNING: the checkout moved to {plan.tip[:12]}, but git exited {p.returncode} "
                f"(a post-checkout hook?): {p.stderr.strip()}"
            )
        else:
            out(f"  checkout: moved to {plan.tip[:12]}.")
        files_moved = True
    # Past the commit point: the checkout has moved (or was already right), so
    # nothing below may raise a refusal that would claim nothing changed.
    state = f"`live` is at {at[:12]}"
    after_move(state, "keeping the reflog of `live` (git config)", lambda: ensure_reflog_kept(repo))
    up = after_move(
        state,
        "setting `live` to track origin/main",
        lambda: repo.git(
            "branch", "--set-upstream-to", f"{BASE_REMOTE}/{BASE_BRANCH}", LIVE_BRANCH, check=False
        ),
    )
    if up is not None and up.returncode != 0:
        out(
            f"  WARNING: could not set `live` to track {BASE_REMOTE}/{BASE_BRANCH}: {up.stderr.strip()}"
        )
    return Move(files_moved, at)


def restore_moved_hooks(repo: Repo, before: str | None, after: str) -> None:
    """Replace each installed git hook whose bytes are exactly that hook as the
    checkout held it BEFORE the move (``before``) with the moved-to version.

    sync-hooks.sh overwrites an installed hook only when its hash is a version
    main has shipped (.genesis-hook-versions). A hook an approved candidate
    installed is not one, so once that candidate leaves `live` (drop, exclusion,
    retirement) sync would keep it as "user-modified" and readiness would then
    refuse every rebuild. A hook equal to the pre-move checkout's copy was put
    there by a sync of that checkout, so this run may replace it; one edited by
    hand equals neither side and is left for sync-hooks.sh to report. The names
    are BOTH checkouts' lists: a hook the old checkout installed is deleted when
    the new checkout no longer installs it (no source, or its list no longer
    names it), since nothing else ever removes an installed hook. Runs before
    sync_git_hooks, after the move;
    failures are reported per hook, never fatal."""
    if not before:
        return
    hooks_dir = repo.hooks_dir()
    if os.path.realpath(hooks_dir) != os.path.realpath(Path(repo.common_dir()) / "hooks"):
        # sync-hooks.sh installs into $GIT_COMMON_DIR/hooks; with core.hooksPath
        # pointing elsewhere readiness already refuses, and this must not write
        # where sync does not.
        out("  NOTE: core.hooksPath is set; installed hooks were not restored.")
        return
    # A hook is managed on a side only when that side's list names it AND its
    # source exists: a name leaving the list is gone even if its file stays (an
    # approved list change enabled another candidate's file, and the list change
    # was dropped). An unreadable list is unknown, never "empty": nothing is
    # restored from an unknown old side, nothing deleted for an unknown new one.
    listed: dict[str, list[str] | None] = {}
    for ref in (before, after):
        try:
            listed[ref] = sync_hook_names(repo.show(ref, SYNC_HOOKS) or "")
        except Refusal as exc:
            listed[ref] = None
            out(
                f"  NOTE: cannot read the hook list at {ref[:12]} ({exc}); its hooks were not restored."
            )
    names: list[str] = []
    for ref in (before, after):
        names += [n for n in listed[ref] or [] if n not in names]
    for name in names:
        if name in (".", ".."):
            continue
        try:
            _restore_one(
                repo,
                hooks_dir / name,
                f"scripts/hooks/{name}",
                before if listed[before] is not None and name in listed[before] else None,
                after,
                after_listed=listed[after] is None or name in listed[after],
            )
        except (OSError, Refusal) as exc:
            out(f"  NOTE: could not restore git hook {name} ({exc}); sync-hooks.sh follows.")


def _restore_one(
    repo: Repo, dst: Path, path: str, before: str | None, after: str, after_listed: bool
) -> None:
    """``before`` is None when the old checkout did not manage this name, and
    ``after_listed`` False when the moved-to list no longer names it."""
    if before is None:
        return
    old = repo.blob_at(before, path)
    new = repo.blob_at(after, path) if after_listed else None
    if old is None or old == new or not dst.is_file():
        return
    if is_symlink_at(repo, before, path) or is_symlink_at(repo, after, path):
        # A link's blob is its target path, not the hook's bytes; sync-hooks.sh
        # copies the referent, so leave a linked hook to it.
        return
    if repo.git("hash-object", "--no-filters", "--", str(dst)).stdout.strip() != old:
        if new is None:
            # sync-hooks.sh will not mention a name its list no longer has.
            out(
                f"  NOTE: git hook {dst.name} is no longer installed by this checkout but "
                "was edited by hand; left in place."
            )
        return  # not the old checkout's copy: edited by hand, or never synced
    if new is None:
        dst.unlink()
        out(f"  removed git hook {dst.name}: the moved-to checkout no longer installs it.")
        return
    tmp = dst.with_name(f"{dst.name}.tmp.{os.getpid()}")
    try:
        # repo.git decodes with surrogateescape: encoding back is byte-exact.
        tmp.write_bytes(repo.git("cat-file", "blob", new).stdout.encode("utf-8", "surrogateescape"))
        tmp.chmod(0o755)
        os.replace(tmp, dst)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    out(f"  restored git hook {dst.name} to the moved-to checkout's version.")


def sync_git_hooks(repo: Repo) -> None:
    """Install the git hooks from the checkout (deploy_code_only.sh's step):
    sync-hooks.sh is idempotent and never overwrites a hook someone modified;
    its non-zero exits are reported, never fatal. On `live` the hook sources,
    sync-hooks.sh itself included, are reviewed main's or an approved
    candidate's: admission refuses any other candidate that changes them, and a
    rebuild or drop excludes any candidate that is not the sole, approved owner of
    a hook path it changes."""
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
