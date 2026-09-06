"""Git atoms for the zero-drop detector — one injectable subprocess seam.

Every call is READ-ONLY. The detector never fetches, pushes, checks out or
prunes: it observes the repository and writes only to its own findings store.
That is a requirement, not an implementation detail — a detector that mutates
the thing it measures cannot be trusted to report on it.

Read-only is enforced by CONSTRUCTION, not by choosing read-only verbs: every
argv is built by ``_git()``, which adds ``--no-optional-locks``. Without it
even ``git status`` writes — it refreshes the stat cache and takes
``index.lock`` — so the claim above was false for the busiest call in the
sweep until it was measured. See that helper for the measurement.

All commands run through a single injectable ``Runner`` (the hermetic tests
drive real fixture repos through the default runner and a fake one through the
seam), and every command is addressed with ``git -C <path>`` rather than a
process cwd — the repo's cwd drifts, and a lost cd would silently point the
sweep at a different worktree.

Each function returns data or ``{"error": ...}``; nothing raises. A failed leg
must degrade the CLASS it feeds (the caller skips that class entirely), never
half-apply: a partial sweep that resolved the branches it never looked at
would manufacture a clean board.

Timeouts (the raw-subprocess-with-no-external-watchdog carve-out in the
timeout policy — a hung git here sits on the detector flock and starves every
later sweep until process death, with nothing attached to notice):

- ref sweep 60s — MEASURED 84ms over 209 refs on this install (2026-09-05),
  so ~700x headroom; the failure mode is a locked/corrupt object store.
- ls-remote 30s — one network round-trip, MEASURED 0.39s.
- per-worktree status 30s — local, but a worktree under a dead network mount
  would hang; 161 worktrees means one hang must not eat the whole sweep.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

Runner = Callable[[list[str], float], Awaitable[tuple[int, str, str]]]

REF_SWEEP_TIMEOUT_S = 60.0
LS_REMOTE_TIMEOUT_S = 30.0
STATUS_TIMEOUT_S = 30.0
# Ancestry/rev-list are pure local object-store reads on two commits that are
# already resolved — no walk of the whole history, no network. They run only
# for branches whose tip differs from the evidence being tested (MEASURED
# 2026-09-06: 10 of 217 refs on this install), so the budget is per-call and
# small; the failure mode is the same locked/corrupt object store as the ref
# sweep, and a hang here would sit on the detector flock.
ANCESTRY_TIMEOUT_S = 30.0

# A git object name as git itself prints it. Used to validate every SHA that
# reaches argv from OUTSIDE this repository (gh JSON, ls-remote output) — see
# is_ancestor. Deliberately full-length: an abbreviated SHA is ambiguous, and
# ambiguity in an identity comparison is how two commits become one.
_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


def _is_sha(value: object) -> bool:
    """True iff *value* is a full hex object name, checking TYPE before shape.

    The type half is not defensive padding. These values arrive as gh JSON
    (`headRefOid` is nullable) and as fields parsed out of git output, so
    ``None`` — or any non-string — is a shape the input can genuinely take, and
    handing one to ``re.match`` raises TypeError rather than returning False.
    That exception would escape the classifier and abort the WHOLE sweep, which
    is strictly worse than the wrong answer it was guarding against: a refused
    value degrades one branch to "unanswerable", while a crash degrades every
    branch to "not looked at". Reject the value; never raise on it.
    """
    return isinstance(value, str) and bool(_FULL_SHA.match(value))


def _refuse_empty(kind: str, records) -> dict | None:
    """Refuse an rc=0 result that parsed to NOTHING. Shared by all enumerators.

    An empty set does NOT fail neutrally, which is what makes this a
    correctness guard rather than tidiness. Each enumerator feeds a class that
    RECONCILES: whatever it does not return is treated as gone, so an empty
    result resolves every open and acknowledged finding in that class at once —
    silently, confidently, completely. That is the false clean board this
    subsystem exists to prevent, arriving through the one door that looks like
    success.

    And empty cannot be a true observation for any of them: a repository always
    has at least one local branch, at least one branch on its remote, and at
    least one worktree. "rc=0 and nothing parsed" therefore means the output was
    not what we think it is — a format change, a wrong path, a truncated read —
    and the honest response is to freeze the class.

    MEASURED 2026-09-06 before this existed: the guard was on ls-remote ONLY.
    ``for-each-ref`` and ``worktree list`` both returned a clean empty set from
    rc=0, and ``worktree list`` did so even for unparseable garbage.
    """
    if not records:
        return {"error": f"{kind} returned no records (rc=0) — refusing an empty set"}
    return None


# TAB-separated so a branch name containing a space survives the split; git ref
# names cannot contain a TAB (check-ref-format forbids control characters).
_REF_FORMAT = "%(refname:short)\t%(objectname)\t%(ahead-behind:{base})\t%(committerdate:iso-strict)"

# `base` is spliced into a git FORMAT STRING, where `%(...)` is a directive. It
# arrives from `refs/remotes/origin/HEAD` — i.e. whatever the remote's default
# branch is named — and a git ref name may legally contain `%`, `(` and `)`. A
# name carrying a format directive would inject extra fields into the output the
# classifier trusts to be four TAB-separated columns. Refuse such a base rather
# than sanitising it: a ref name that cannot be safely formatted is not a value
# we accept, and the caller falls back to the documented default and says so.
_SAFE_BASE_REF = re.compile(r"^[A-Za-z0-9._/@+-]{1,255}$")


def is_safe_base_ref(base: str | None) -> bool:
    """True iff *base* can be spliced into a git format string unambiguously."""
    return bool(base) and bool(_SAFE_BASE_REF.match(base))


def _git(root: str, *args: str) -> list[str]:
    """Build a git argv that CANNOT write to the repository it is reading.

    ``--no-optional-locks`` is the load-bearing part, and the module docstring
    above was simply wrong without it. MEASURED on git 2.43: a plain
    ``git status --porcelain`` REWRITES ``.git/index`` whenever a tracked
    file's mtime has moved — it refreshes the stat cache and takes
    ``index.lock`` to do it — while the same command with this flag does not.
    Across ~161 worktrees per sweep, on a box where other sessions are running
    their own git, that is 161 lock acquisitions contending with live work, to
    answer a question that changes nothing.
    (Git added the flag in 2.15 for exactly this caller: a poller that wants to
    observe a repository without touching it.)

    A helper rather than a flag repeated at six call sites, for the reason this
    subsystem keeps rediscovering: an obligation every call site must REMEMBER
    is a convention, and a convention is what a reviewer finds one missing
    instance of. Routed through here, forgetting is not expressible.
    """
    return ["git", "--no-optional-locks", "-C", root, *args]


async def default_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run a git command, returning (rc, stdout, stderr). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:  # git missing / not executable
        return 127, "", f"git spawn failed: {exc}"
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"git call timed out after {timeout}s"
    return (
        proc.returncode or 0,
        stdout.decode(errors="replace"),
        stderr.decode(errors="replace"),
    )


async def list_local_branches(
    root: str, *, base: str = "origin/main", runner: Runner | None = None
) -> dict:
    """Every local branch with its tip, ahead/behind vs ``base``, and tip date.

    ONE ``for-each-ref`` gives the whole candidate set — no per-branch
    ``rev-list``. ``%(ahead-behind:)`` needs git >= 2.41 (verified on the
    2.43 shipped here); on an older git the field expands empty and the
    branch is reported with ``ahead=None``, which the classifier treats as
    unknown (never as zero — an unknown ahead-count must not read as "this
    branch has nothing on it").

    Returns ``{"branches": [{branch, tip_sha, ahead, behind, tip_date}]}``.
    """
    if not is_safe_base_ref(base):
        return {"error": f"unsafe base ref for a git format string: {base[:80]!r}"}
    run = runner or default_runner
    rc, out, err = await run(
        _git(root, "for-each-ref", "refs/heads", "--format", _REF_FORMAT.format(base=base)),
        REF_SWEEP_TIMEOUT_S,
    )
    if rc != 0:
        return {"error": f"for-each-ref failed (rc={rc}): {err.strip()[:300]}"}
    branches = []
    unparsed = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[0]:
            unparsed += 1
            continue
        name, tip, ahead_behind, tip_date = parts
        ahead: int | None = None
        behind: int | None = None
        bits = ahead_behind.split()
        if len(bits) == 2:
            try:
                ahead, behind = int(bits[0]), int(bits[1])
            except ValueError:
                ahead = behind = None
        branches.append(
            {
                "branch": name,
                "tip_sha": tip,
                "ahead": ahead,
                "behind": behind,
                "tip_date": tip_date or None,
            }
        )
    if unparsed:
        # A ref we could not read is a ref we did not enumerate. Failing the
        # whole leg is correct here: the branch classes reconcile against a
        # COMPLETE candidate set, and a quietly-short one resolves findings for
        # branches that were simply never listed.
        return {"error": f"for-each-ref: {unparsed} unparseable ref line(s)"}
    if err := _refuse_empty("for-each-ref", branches):
        return err
    return {"branches": branches}


async def list_remote_heads(
    root: str, *, remote: str = "origin", runner: Runner | None = None
) -> dict:
    """Branch name -> tip SHA on the remote RIGHT NOW (live ls-remote).

    Deliberately not ``refs/remotes/<remote>``: that mirror is only as fresh
    as the last fetch, so a branch pushed by another session would read as
    never-pushed and land in the wrong class — and class is part of a
    finding's identity, so a misclassification creates a duplicate row rather
    than a corrected one.

    The SHA is the point, and an earlier version of this function threw it
    away. ``ls-remote`` answers with ``<sha>\\t refs/heads/<name>``; keeping
    only the name reduces the strongest evidence available — *is this exact
    commit on the server* — to the weakest, *does something with this name
    exist there*. Local-tip != remote-tip is a direct, clock-free, name-free
    test for commits that exist nowhere but this machine, which is the
    condition this whole detector was built to find.

    Returns ``{"heads": {name: sha}}``.
    """
    run = runner or default_runner
    rc, out, err = await run(_git(root, "ls-remote", "--heads", remote), LS_REMOTE_TIMEOUT_S)
    if rc != 0:
        return {"error": f"ls-remote failed (rc={rc}): {err.strip()[:300]}"}
    heads: dict[str, str] = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/") and _FULL_SHA.match(sha):
            heads[ref[len("refs/heads/") :]] = sha
    # On top of the class-wide resolve that `_refuse_empty` describes, an empty
    # set fails a second way here: it reclassifies EVERY branch as never-pushed,
    # and class is part of a finding's identity, so it forks rows instead of
    # correcting them.
    if err := _refuse_empty("ls-remote", heads):
        return err
    return {"heads": heads}


async def is_ancestor(
    root: str, ancestor: str, descendant: str, *, runner: Runner | None = None
) -> bool | None:
    """Is *ancestor* reachable from *descendant*? ``None`` when unanswerable.

    Three-valued on purpose. ``git merge-base --is-ancestor`` exits 0 for yes
    and 1 for no, but 128 when an object is simply not in this repository —
    which happens routinely here, because a merged PR's head SHA may have been
    pushed from another machine and never fetched. Folding that into False
    would turn "I cannot see that commit" into "those commits are stranded",
    which is a confident answer built on absent evidence. The caller
    distinguishes the three and HOLDS on ``None``.

    Both arguments are validated as full hex SHAs before reaching argv. They
    come from ``gh`` JSON and from ``ls-remote`` output — neither is ours to
    trust — and a value like ``--upload-pack=…`` reaching a subprocess is a
    different class of problem than a wrong verdict. Same reasoning as
    ``is_safe_base_ref``, one boundary over.
    """
    if not (_is_sha(ancestor) and _is_sha(descendant)):
        logger.warning("zero_drop is_ancestor refused a non-SHA argument")
        return None
    run = runner or default_runner
    rc, _, err = await run(
        _git(root, "merge-base", "--is-ancestor", ancestor, descendant),
        ANCESTRY_TIMEOUT_S,
    )
    if rc == 0:
        return True
    if rc == 1:
        return False
    logger.debug("zero_drop is_ancestor unanswerable (rc=%s): %s", rc, err.strip()[:200])
    return None


async def count_non_merge_commits(
    root: str, exclude: str, include: str, *, runner: Runner | None = None
) -> int | None:
    """Commits in *include* but not *exclude*, ignoring merges. None if unknown.

    The merge commits are excluded because they carry no unique work: a branch
    whose only local-only commits are merges of the base branch has diverged by
    ancestry while holding nothing that exists nowhere else, and flagging it
    would be noise sitting on top of a real signal. A count of 0 therefore
    means "diverged, but every distinct commit is already on the remote".
    """
    if not (_is_sha(exclude) and _is_sha(include)):
        logger.warning("zero_drop count_non_merge_commits refused a non-SHA argument")
        return None
    run = runner or default_runner
    rc, out, err = await run(
        _git(root, "rev-list", "--count", "--no-merges", f"{exclude}..{include}"),
        ANCESTRY_TIMEOUT_S,
    )
    if rc != 0:
        logger.debug("zero_drop rev-list failed (rc=%s): %s", rc, err.strip()[:200])
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


async def list_worktrees(root: str, *, runner: Runner | None = None) -> dict:
    """Every worktree of this repo as ``{path, branch, detached, prunable}``.

    ``--porcelain`` records are blank-line separated; a detached worktree has
    no ``branch`` line.

    ``prunable`` matters more than it looks. MEASURED on git 2.43: when a
    worktree's directory is deleted, the registration survives and the listing
    carries ``prunable gitdir file points to non-existent location``, while
    ``git -C <that path> status`` fails rc=128. Reading that failure as "I
    could not look" would freeze the whole dirty class on every sweep from then
    on — one stale registration blinding a class permanently. A prunable
    worktree is not unreadable, it is GONE, and a directory that does not exist
    holds no uncommitted work; the caller skips it and counts it.
    """
    run = runner or default_runner
    rc, out, err = await run(_git(root, "worktree", "list", "--porcelain"), REF_SWEEP_TIMEOUT_S)
    if rc != 0:
        return {"error": f"worktree list failed (rc={rc}): {err.strip()[:300]}"}
    worktrees: list[dict] = []
    current: dict = {}
    unparsed = 0
    for line in out.splitlines():
        if line.startswith("worktree "):
            if current.get("path"):
                worktrees.append(current)
            current = {
                "path": line[len("worktree ") :],
                "branch": None,
                "detached": False,
                "prunable": None,
            }
        elif line.startswith("branch refs/heads/"):
            current["branch"] = line[len("branch refs/heads/") :]
        elif line.strip() == "detached":
            current["detached"] = True
        elif line.startswith("prunable"):
            current["prunable"] = line[len("prunable") :].strip() or "prunable"
        elif line.strip() and not line.startswith(("HEAD ", "bare", "locked", "branch ")):
            # A record shape we do not recognise. The two sibling enumerators
            # have counted these from the start and this one did not, so a
            # format change here would have SHRUNK the listing rather than
            # failing it — and a shorter listing resolves the worktrees it
            # silently dropped. Known-but-unused keys are named above rather
            # than swept into this counter, so the check stays honest.
            unparsed += 1
    if current.get("path"):
        worktrees.append(current)
    if unparsed:
        return {"error": f"worktree list: {unparsed} unrecognised record line(s)"}
    if err := _refuse_empty("worktree list", worktrees):
        return err
    return {"worktrees": worktrees}


def parse_status_z(payload: str) -> tuple[list[tuple[str, str]], int]:
    """Parse ``git status --porcelain -z`` into ``([(xy, path), ...], unparsed)``.

    ``-z`` rather than the newline form on purpose: the default porcelain
    output C-quotes any path with a space, a quote or a non-ASCII byte, so a
    line-based parse silently mangles exactly the paths most likely to be
    somebody's untracked work. With ``-z`` the path is emitted RAW.

    A rename/copy entry (``R``/``C``) is followed by its ORIGIN path as a
    second NUL-terminated field with no status prefix; that field is consumed
    here and dropped — the destination is the path that exists on disk, which
    is what the age gate stats.
    """
    fields = [f for f in payload.split("\0") if f]
    entries: list[tuple[str, str]] = []
    unparsed = 0
    skip_next = False
    for field in fields:
        if skip_next:
            skip_next = False
            continue
        if len(field) < 4 or field[2] != " ":
            # Not an entry header. Never guessed at — and never SILENTLY
            # dropped either: an unreadable record is "I could not see this",
            # which for a detector must degrade the class, not shrink to a
            # clean worktree. The count is what lets the caller tell the two
            # apart; discarding it made a garbled status indistinguishable
            # from no changes at all.
            unparsed += 1
            continue
        xy, path = field[:2], field[3:]
        entries.append((xy, path))
        if xy[0] in ("R", "C") or xy[1] in ("R", "C"):
            skip_next = True
    return entries, unparsed


async def worktree_status(path: str, *, runner: Runner | None = None) -> dict:
    """Uncommitted state of one worktree as ``{"entries": [(xy, path), ...]}``.

    Untracked files count. An untracked source file IS stranded work — the
    exact shape of "I wrote it and never added it" — and this repo gitignores
    its build/temp output, so the noise floor is low.
    """
    run = runner or default_runner
    rc, out, err = await run(_git(path, "status", "--porcelain", "-z"), STATUS_TIMEOUT_S)
    if rc != 0:
        return {"error": f"status failed (rc={rc}): {err.strip()[:200]}"}
    entries, unparsed = parse_status_z(out)
    if unparsed:
        # A successful CALL with unreadable OUTPUT is still a failed read. The
        # rc check above only catches the former; without this, a garbled
        # porcelain stream reported a CLEAN worktree.
        return {"error": f"status: {unparsed} unparseable porcelain record(s)"}
    return {"entries": entries}
