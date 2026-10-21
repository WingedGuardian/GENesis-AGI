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

import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable
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
from deploy_candidates_gate import (  # noqa: E402
    HELPER_ARRAYS,
    SYNC_HOOKS,
    non_file_kind,
    sync_hook_names,
)


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
    """What a move did: whether the checkout's files changed, the commit `live`
    is at afterwards (its old tip when nothing changed, else the plan's tip), and
    the commit HEAD was at before (the source's head, checked under the lock).
    Every post-move WARNING names ``at``, never a commit that is not live."""

    files: bool
    at: str
    before: str | None
    head_left: str | None = None  # where HEAD went if it left `live` in the last instant


class Source(NamedTuple):
    """Where the checkout was when the command read it (#3492): the ref HEAD is
    on (None: detached), the commit HEAD names, the commit `live` names, another
    worktree with `live` checked out (None: none), and the tracked changes
    (dirty_lines: the excused paths stay excused). Before a move, preflight has
    refused any tracked change and another worktree on `live`, so the last two
    are empty. Every write and every success return re-checks all five, because
    a session can commit, switch or edit at any time and takes no Genesis lock."""

    branch_ref: str | None
    head: str | None
    live_ref: str | None
    elsewhere: str | None
    dirty: tuple[str, ...]


def read_source(repo: Repo) -> Source:
    p = repo.git("symbolic-ref", "-q", "HEAD", check=False)
    if p.returncode not in (0, 1):  # 1: a detached HEAD; anything else: git cannot say
        raise repo._unknown("which branch is checked out", p)
    ref = p.stdout.strip() if p.returncode == 0 else ""
    return Source(
        ref or None,
        repo.resolve("HEAD"),
        repo.resolve(LIVE_REF),
        repo.live_checked_out_elsewhere(),
        tuple(repo.dirty_lines()),
    )


#: What a manifest-only change (a drop off `live`, or --no-rebuild) depends on:
#: it never touches the working tree, so tracked edits there do not concern it.
REF_FIELDS = ("branch_ref", "head", "live_ref")
_FIELD_NAMES = {
    "branch_ref": "HEAD's branch",
    "head": "HEAD",
    "live_ref": "`live`",
    "elsewhere": "another worktree with `live`",
}


def source_mismatch(
    repo: Repo, source: Source, fields: tuple[str, ...] = Source._fields
) -> str | None:
    """How the checkout now differs from ``source`` in ``fields``, or None."""
    now = read_source(repo)
    for field in fields:
        was, is_ = getattr(source, field), getattr(now, field)
        if was == is_:
            continue
        if field == "dirty":
            return "tracked files changed:\n" + "\n".join("  " + ln for ln in now.dirty)
        return f"{_FIELD_NAMES[field]} is now {is_ or 'none'}, not {was or 'none'}"
    return None


def moved_source_refusal(why: str, unchanged: str) -> Refusal:
    return Refusal(
        f"the checkout is not where its caller left it ({why}); someone else changed it "
        f"meanwhile; {unchanged}. Keep that work, then retry."
    )


def refuse_moved_source(
    repo: Repo,
    source: Source,
    unchanged: str = "nothing moved",
    fields: tuple[str, ...] = Source._fields,
) -> None:
    why = source_mismatch(repo, source, fields)
    if why:
        raise moved_source_refusal(why, unchanged)


_CHECKOUT_LOCK_WAIT_S = 300.0
_CHECKOUT_LOCK_POLL_S = 0.25
# ASSUMED, unmeasured bound


def _acquire_checkout_lock(repo: Repo) -> int:
    try:
        result = repo.git(
            "rev-parse", "--path-format=absolute", "--git-common-dir", check=False, timeout=2
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Refusal(f"cannot resolve checkout lock path; nothing moved: {exc}") from exc
    common_dir = result.stdout.strip()
    if result.returncode != 0 or not common_dir:
        raise Refusal(f"cannot resolve checkout lock path; nothing moved: {result.stderr.strip()}")
    path = Path(common_dir).resolve() / "genesis-checkout.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise Refusal(f"cannot open checkout lock {path}; nothing moved: {exc}") from exc
    deadline = time.monotonic() + _CHECKOUT_LOCK_WAIT_S
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Refusal(
                        "checkout busy (a Claude launch holds genesis-checkout.lock); nothing moved"
                    ) from None
                time.sleep(min(_CHECKOUT_LOCK_POLL_S, remaining))
    except BaseException:
        os.close(fd)
        raise


#: Where a caller (update.sh on `live`) asks for the move record: a JSON file
#: {"to": <tip>, "phase": <phase>} saying how far the move got, so the caller
#: never infers from git afterwards what the move did. Phases: "ref" (the ref is
#: about to move, the tree is unchanged), "moving" (the switch is about to run),
#: "partial" (git wrote part of the tree and failed), "clean" (git refused and
#: wrote nothing), "done" (the checkout is at "to"). No variable, no record.
MOVE_RECORD_ENV = "DEPLOY_CANDIDATES_MOVE_RECORD"


def _write_move_record(repo: Repo, to: str, phase: str, *, before: bool) -> None:
    """Write the move record atomically (same-directory temp + os.replace).
    Before the move a failure refuses (nothing has moved: a missing record must
    keep meaning "the engine never moved"); after it, a failure only warns."""
    path = repo.env.get(MOVE_RECORD_ENV)
    if not path:
        return
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"to": to, "phase": phase}, fh)
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        if before:
            raise Refusal(f"cannot write the move record {path} ({exc}); nothing moved") from exc
        out(
            f"  WARNING: the checkout is at {to[:12]}, but the move record {path} could not be written: {exc}"
        )


def move_checkout(
    repo: Repo, plan: Plan, source: Source, before_write: Callable[[], None] | None = None
) -> Move:
    lock_fd = _acquire_checkout_lock(repo)
    try:
        return _move_checkout_locked(repo, plan, source, before_write or (lambda: None))
    finally:
        os.close(lock_fd)


def _move_checkout_locked(
    repo: Repo, plan: Plan, source: Source, before_write: Callable[[], None]
) -> Move:
    """Point the checkout at ``plan.tip``: not at all when nothing changed, the
    ref alone when only commits changed, otherwise ONE `git switch`. Returns
    whether the checkout's files changed and where `live` is (a Move). Git
    configuration for `live` is
    written only after the move succeeded, so a refused move writes nothing; a
    failure after the move is a WARNING (core.after_move), never a refusal.
    The checkout must still be at ``source``: checked in full, under the
    lock, just before each path's first write (a move record, the ref or the
    switch) or its unchanged result. A mismatch writes nothing. The caller's
    ``before_write`` (drop's manifest change) runs right after that check, so
    it happens only while the source holds."""
    branch = source.branch_ref.removeprefix("refs/heads/") if source.branch_ref else None
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
        refuse_moved_source(repo, source)  # update.sh restarts on this tree
        before_write()
        at = cur_tip  # nothing moves: `live` stays where it is
        out(f"  checkout: unchanged ({cur_tip[:12]}): same origin/main, same candidate heads.")
    elif branch == LIVE_BRANCH and cur_tip and repo.tree(cur_tip) == repo.tree(plan.tip):
        # Same files under new commits: move the ref, not the working tree.
        refuse_moved_source(repo, source)
        before_write()
        _write_move_record(repo, plan.tip, "ref", before=True)
        p = repo.git(
            "update-ref",
            "-m",
            f"deploy-candidates: rebuild (tree unchanged) onto {plan.base[:12]}",
            LIVE_REF,
            plan.tip,
            cur_tip,
            check=False,
        )
        if p.returncode != 0:  # the compare-and-swap: `live` is no longer cur_tip
            _write_move_record(repo, plan.tip, "clean", before=False)
            raise moved_source_refusal(f"`live` moved: {p.stderr.strip()}", "nothing moved")
        _write_move_record(repo, plan.tip, "done", before=False)
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
        refuse_moved_source(repo, source)
        before_write()
        _write_move_record(repo, plan.tip, "moving", before=True)
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
                    _write_move_record(repo, plan.tip, "partial", before=False)
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
                _write_move_record(repo, plan.tip, "clean", before=False)
                raise Refusal(
                    f"git refused to move the checkout to {plan.tip[:12]}; nothing moved:\n{p.stderr.strip()}"
                )
            out(
                f"  WARNING: the checkout moved to {plan.tip[:12]}, but git exited {p.returncode} "
                f"(a post-checkout hook?): {p.stderr.strip()}"
            )
        else:
            out(f"  checkout: moved to {plan.tip[:12]}.")
        _write_move_record(repo, plan.tip, "done", before=False)
        files_moved = True
        after_move(
            f"`live` is at {at[:12]}",
            "reading the reflog of `live`",
            lambda: _warn_live_slipped(repo, source, plan.tip),
        )
        after_move(
            f"`live` is at {at[:12]}",
            "reading HEAD's reflog",
            lambda: _warn_head_slipped(repo, source),
        )
    # Past the commit point: the checkout has moved (or was already right), so
    # nothing below may raise a refusal that would claim nothing changed.
    state = f"`live` is at {at[:12]}"
    head_left = after_move(state, "reading where HEAD is", lambda: _head_left_live(repo))
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
    return Move(files_moved, at, source.head, head_left or None)


def _head_left_live(repo: Repo) -> str:
    """Every arm leaves HEAD on `live` (#3492): the unchanged and ref-only arms
    require it at the re-check, the switch puts it there. A session that
    switched branch after the last re-check moves nothing back (git's
    update-ref guards only `live` itself, not symbolic HEAD), so it is named,
    and the caller replaces its restart advice. Returns where HEAD went, or ""
    when it is on `live`. Never refuses."""
    p = repo.git("symbolic-ref", "-q", "HEAD", check=False)
    ref = p.stdout.strip()
    if ref == LIVE_REF:
        return ""
    where = (
        ref.removeprefix("refs/heads/")
        if ref
        else f"a detached {(repo.resolve('HEAD') or 'HEAD')[:12]}"
    )
    out(
        f"  WARNING: HEAD is now on {where}, not `live`: a session switched branch while this "
        "ran. `live` is as reported; the checkout is not."
    )
    return where


def _warn_live_slipped(repo: Repo, source: Source, tip: str) -> None:
    """The residual window (#3492): between the pre-write re-check and git taking
    its index.lock, a session commit can still land, and `switch -C` resets
    `live` over it. The reflog says what `live` was when the switch ran; a value
    other than the source's is named, never refused (the move has happened).

    Blind spot: when `live` already sat at ``tip`` (only reachable from main: on
    `live` that tip would be the unchanged path), `switch -C live <tip>` logs
    nothing, so `live@{1}` is an older value and a slip there cannot be told
    from it; that case is not checked. A tracked edit in the window is not
    seen at all: only a commit leaves a reflog entry. When `live` did not exist,
    the switch's own creation is its only entry: an older one means a session
    created it in the window."""
    if source.live_ref == tip:
        return  # reset to itself: git logged nothing to compare
    p = repo.git("rev-parse", "--verify", "-q", f"{LIVE_REF}@{{1}}^{{commit}}", check=False)
    was = p.stdout.strip()
    if source.live_ref is None:
        if p.returncode == 0 and was:
            out(
                f"  WARNING: live was at {was[:12]}, not absent, when the move ran; "
                f"{was[:12]} is kept in live's reflog."
            )
    elif p.returncode != 0 or not was:
        out(
            f"  NOTE: the reflog of `live` could not be read; it was {source.live_ref[:12]} before."
        )
    elif was != source.live_ref:
        out(
            f"  WARNING: live was at {was[:12]}, not {source.live_ref[:12]}, when the move ran; "
            f"{was[:12]} is kept in live's reflog."
        )


def _warn_head_slipped(repo: Repo, source: Source) -> None:
    """HEAD's reflog says where HEAD was when the switch ran (#3492): its own
    last entries are "checkout: moving from <branch> to live", preceded on
    `live` by "branch: Reset to <tip>" (git writes both, MEASURED 2.43), and
    the entry before them is HEAD then. A session that switched branch in the
    window, or committed on main (which `live`'s reflog cannot see), is named;
    a commit on `live` is left to _warn_live_slipped. Never refuses."""
    p = repo.git("reflog", "-3", "--format=%H%x09%gs", "HEAD", check=False)
    entries = [ln.split("\t", 1) for ln in p.stdout.splitlines() if "\t" in ln]
    prefix, suffix = "checkout: moving from ", f" to {LIVE_BRANCH}"
    ours = 1
    if (
        len(entries) >= 2
        and entries[0][1] == f"{prefix}{LIVE_BRANCH}{suffix}"
        and entries[1][1].startswith("branch: Reset to ")
    ):
        ours = 2
    if p.returncode != 0 or len(entries) <= ours or not entries[0][1].startswith(prefix):
        out("  NOTE: HEAD's reflog could not be read; where HEAD was is not checked.")
        return
    was_branch = entries[0][1][len(prefix) :].removesuffix(suffix)
    was_head = entries[ours][0]
    want_branch = (source.branch_ref or "").removeprefix("refs/heads/")
    if was_branch != want_branch or (want_branch != LIVE_BRANCH and was_head != source.head):
        out(
            f"  WARNING: HEAD was on {was_branch} at {was_head[:12]}, not {want_branch} at "
            f"{(source.head or 'none')[:12]}, when the move ran; that work is kept "
            "(its branch, or HEAD's reflog)."
        )


def restore_moved_hooks(repo: Repo, before: str | None, after: str) -> list[str]:
    """Replace each installed git hook whose bytes are exactly that hook as the
    checkout held it BEFORE the move (``before``) with the moved-to version.

    sync-hooks.sh overwrites an installed hook only when its hash is a version
    main has shipped (.genesis-hook-versions). A hook an approved candidate
    installed is not one, so once that candidate leaves `live` (drop, exclusion,
    retirement) sync would keep it as "user-modified" and readiness would then
    refuse every rebuild. A hook equal to the pre-move checkout's copy was put
    there by a sync of that checkout, so this run may replace it; one edited by
    hand equals neither side and is left in place. The names
    are BOTH checkouts' lists: a hook the old checkout installed is deleted when
    the new checkout no longer installs it (no source, or its list no longer
    names it), since nothing else ever removes an installed hook. Runs before
    sync_git_hooks, after the move. Returns why it could not leave a hook as the
    moved-to checkout installs it ([] when it could), a hand-edited hook
    included: the caller decides whether that fails it (restore-hooks) or is
    a warning past the commit point (rebuild, drop)."""
    if not before:
        return []
    hooks_dir = repo.hooks_dir()
    if os.path.realpath(hooks_dir) != os.path.realpath(Path(repo.common_dir()) / "hooks"):
        # sync-hooks.sh installs into $GIT_COMMON_DIR/hooks; with core.hooksPath
        # pointing elsewhere readiness already refuses, and this must not write
        # where sync does not.
        return ["core.hooksPath is set; installed hooks were not restored"]
    # A hook is managed on a side only when that side's list names it AND its
    # source exists: a name leaving the list is gone even if its file stays (an
    # approved list change enabled another candidate's file, and the list change
    # was dropped). An unreadable list is unknown, never "empty": nothing is
    # restored from an unknown old side, nothing deleted for an unknown new one.
    failures: list[str] = []
    listed: dict[str, list[str] | None] = {}
    helpers: list[str] = []
    for ref in (before, after):
        try:
            listed[ref] = sync_hook_names(repo.show(ref, SYNC_HOOKS) or "")
            if ref == after:
                helpers = sync_hook_names(repo.show(ref, SYNC_HOOKS) or "", HELPER_ARRAYS)
        except Refusal as exc:
            listed[ref] = None
            failures.append(
                f"cannot read the hook list at {ref[:12]} ({exc}); its hooks were not restored"
            )
    names: list[str] = []
    for ref in (before, after):
        names += [n for n in listed[ref] or [] if n not in names]
    for name in names:
        if name in (".", ".."):
            continue
        try:
            why = _restore_one(
                repo,
                hooks_dir / name,
                f"scripts/hooks/{name}",
                before if listed[before] is not None and name in listed[before] else None,
                after,
                after_listed=listed[after] is None or name in listed[after],
                helper=name in helpers,
            )
        except (OSError, Refusal) as exc:
            why = f"could not restore git hook {name} ({exc})"
        if why:
            failures.append(why)
    return failures


def _restore_one(
    repo: Repo,
    dst: Path,
    path: str,
    before: str | None,
    after: str,
    after_listed: bool,
    helper: bool = False,
) -> str | None:
    """``before`` is None when the old checkout did not manage this name, and
    ``after_listed`` False when the moved-to list no longer names it; ``helper``
    is True for a name in the moved-to HELPERS_TO_SYNC. Returns why the hook was
    left as neither checkout installs it, or None."""
    new_kind = non_file_kind(repo, after, path)
    if (
        helper
        and dst.is_file()
        and new_kind != "symbolic link"
        and (new_kind or repo.blob_at(after, path) is None)
    ):
        # A helper is Genesis's own and the moved-to checkout has no source for
        # it, so sync-hooks.sh skips it: removed here, as a hook the moved-to
        # list drops is (a failure to remove it is the caller's failure).
        dst.unlink()
        out(f"  removed git helper {dst.name}: the moved-to checkout has no source for it.")
        return None
    if before is None:
        return None
    if non_file_kind(repo, before, path):
        return None  # the old copy came from sync-hooks.sh (a link's target) or nowhere
    old = repo.blob_at(before, path)
    if new_kind == "symbolic link":
        return None  # sync-hooks.sh installs what a link points at; leave it to sync
    # A directory or submodule installs nothing (sync-hooks.sh skips it), so the
    # moved-to side counts as having no source for this hook.
    new = repo.blob_at(after, path) if after_listed and not new_kind else None
    if old is None or old == new or not dst.is_file():
        return None
    have = repo.git("hash-object", "--no-filters", "--", str(dst)).stdout.strip()
    if have == new:
        return None  # already the moved-to checkout's copy
    # A hash .genesis-hook-versions lists is a version Genesis shipped: written
    # by Genesis, like the old checkout's copy, so never a hand edit.
    shipped = have != old and _shipped_version(repo, dst)
    if have != old and new is not None and (helper or shipped):
        # sync-hooks.sh overwrites a helper always, and a shipped version: the
        # post-condition decides.
        return None
    if have != old and not shipped:
        # Edited by hand (or never synced): left in place, and reported even when
        # the moved-to list no longer names it, which no later check would.
        # Readiness refuses any such hook before a rebuild moves anything.
        unlisted = " (the moved-to checkout no longer installs it)" if new is None else ""
        return f"git hook {dst.name} was edited by hand{unlisted}; left in place"
    if new is None:
        dst.unlink()
        out(f"  removed git hook {dst.name}: the moved-to checkout no longer installs it.")
        return None
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
    return None


def _shipped_version(repo: Repo, dst: Path) -> bool:
    """Whether sync-hooks.sh would call the installed hook a stale prior version
    and replace it: its own test, `name:<sha256>` in the checkout's
    .genesis-hook-versions."""
    try:
        listed = (repo.root / ".genesis-hook-versions").read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    return f"{dst.name}:{hashlib.sha256(dst.read_bytes()).hexdigest()}" in listed


def sync_git_hooks(repo: Repo) -> str | None:
    """Install the git hooks from the checkout (deploy_code_only.sh's step):
    sync-hooks.sh is idempotent and never overwrites a hook someone modified.
    Returns why it did not complete (missing, could not run, timed out, any
    non-zero exit, 2 included), or None; its exit 0 does not prove every copy
    landed (hook_install_failures checks that). On `live` the hook sources,
    sync-hooks.sh itself included, are reviewed main's or an approved
    candidate's: admission refuses any other candidate that changes them, and a
    rebuild or drop excludes any candidate that is not the sole, approved owner of
    a hook path it changes."""
    script = repo.root / "scripts" / "hooks" / "sync-hooks.sh"
    if not script.is_file():
        return f"{script} is missing; the git hook copies were not synced"
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
        return f"sync-hooks.sh could not run ({exc}); the git hooks are as they were"
    said = f": {p.stderr.strip()}" if p.stderr.strip() else ""  # it names the hook
    if rc == 2:
        return f"sync-hooks.sh left a user-modified git hook alone (exit 2){said}"
    if rc:
        return f"sync-hooks.sh could not sync the git hooks (exit {rc}){said}"
    out("  Git hook copies in sync.")
    return None


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
