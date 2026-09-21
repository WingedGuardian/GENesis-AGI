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
import os
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


def _refuse_empty(kind: str, records, *, raw: str) -> dict | None:
    """Refuse output that PARSED to nothing. An empty OUTPUT is not that.

    An empty set does NOT fail neutrally, which is what makes this a
    correctness guard rather than tidiness. Each enumerator feeds a class that
    RECONCILES: whatever it does not return is treated as gone, so an empty
    result resolves every open and acknowledged finding in that class at once —
    silently, confidently, completely. That is the false clean board this
    subsystem exists to prevent, arriving through the one door that looks like
    success.

    But the first version of this guard could not tell the two rc=0 empties
    apart, and they mean opposite things (Codex P2, PR #1794):

    * **rc=0, and git printed NOTHING.** A true observation of an empty set. An
      unborn repository has no local refs; a newly created or fully cleared
      remote answers ``ls-remote`` with no heads at all. MEASURED 2026-09-13 on
      git 2.43: an empty bare repo gives ``ls-remote --heads`` rc=0 and stdout
      of EXACTLY zero bytes, and an unborn repo gives ``for-each-ref
      refs/heads`` the same — and ``--exit-code``, the OPTIONAL flag that turns
      no-match into rc=2, is what this code would have to pass for emptiness to
      be an error at all. Refusing these froze both branch classes forever on a
      condition that never changes, and in the cleared-remote case every local
      branch is precisely the unpushed work the detector exists to report.
    * **rc=0, git printed SOMETHING, and none of it parsed.** The format changed
      under us, or the read was truncated. Nothing here can be trusted and the
      class freezes.

    So the discriminator is the RAW output, not the record count. Passing the
    parsed list alone is what made the two indistinguishable.

    Tested on the raw string rather than ``raw.strip()``, and the measurement
    above is why: an empty set is zero bytes, so WHITESPACE is not one — it is
    output that parsed to nothing, and it belongs on the freezing side. Using
    ``strip()`` would have quietly moved a whole class of unreadable output into
    the permissive branch, which is the direction that resolves findings.

    MEASURED 2026-09-06 before this existed at all: the guard was on ls-remote
    ONLY. ``for-each-ref`` and ``worktree list`` both returned a clean empty set
    from rc=0, and ``worktree list`` did so even for unparseable garbage.

    Where it actually fires, stated because the obvious reading is wrong: each
    enumerator counts unreadable lines itself and returns before reaching here,
    so for a non-empty output that parsed to nothing it is the caller's
    ``unparsed`` counter that freezes the class, not this. That makes this a
    BACKSTOP for a parser that ever stops counting — worth keeping, worth not
    mistaking for the primary guard.
    """
    if records or not raw:
        return None
    return {
        "error": (
            f"{kind}: rc=0 with {len(raw)} bytes of output, none of which parsed "
            "as a record — refusing a set we cannot read"
        )
    }


# TAB-separated so a branch name containing a space survives the split; git ref
# names cannot contain a TAB (check-ref-format forbids control characters).
#
# `lstrip=2`, not `:short`, and the difference is not cosmetic. `:short` emits
# the shortest UNAMBIGUOUS name, so the moment a tag shares a branch's name it
# emits `heads/foo` where the branch is `foo` (MEASURED on git 2.43). The remote
# and the PR history both key on `foo`, so such a branch matches NEITHER the
# ls-remote join nor the PR-name join and is reported as unpushed work with no
# PR. Since this enumeration is scoped to `refs/heads`, the prefix is a known
# constant: a fixed strip is exact where an abbreviation is context-sensitive.
# The identity is also the ack key, so a name that changes shape when an
# unrelated tag appears would expire a standing acknowledgement.
_REF_FORMAT = (
    "%(refname:lstrip=2)\t%(objectname)\t%(ahead-behind:{base})\t%(committerdate:iso-strict)"
)

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


# Git's repository-discovery environment OVERRIDES `-C`. `-C` is equivalent to
# `cd`, and `GIT_DIR` takes precedence over discovery from the working
# directory — so with one of these inherited, every argv `_git()` builds reads a
# repository the caller never named, and the module docstring's claim that
# addressing by `-C` cannot point the sweep at the wrong worktree is simply
# false.
#
# That used to fail CLOSED: a mismatched GIT_DIR yielding zero refs hit
# `_refuse_empty` and froze the branch classes, loudly. Since an rc=0 empty set
# became a legitimate observation it fails OPEN instead — `{"branches": []}`
# reconciles both branch classes against nothing and RESOLVES every open and
# acked finding in them. Stripped in the runner rather than at each call site,
# for the same reason `--no-optional-locks` lives in `_git()`: a guarantee every
# caller must remember is a convention.
#
# FOUR COPIES OF THIS SET EXIST and are kept identical BY TEST, not by import:
# here, `scripts/review_state.py`, `scripts/review_scope.py`, and the bash array
# in `.claude/hooks/genesis-hook`. The hook-side copies cannot import this module
# — hook scripts are stdlib-only, and `git_push_guard` already wraps its
# `review_state` import in `try/except` because a module-load exception in a hook
# exits 1, which Claude Code treats as NON-BLOCKING and which would silently
# disable every fail-closed gate in that file.
# `tests/test_hooks/test_git_env_scrub.py` asserts all four are EQUAL.
#
# The launcher carrying the same names is also what `git_push_guard` needs: it
# PREDICTS what a `git push` will do and must see the config that push will see.
# MEASURED 2026-09-19: scrubbing the user's config there turns a prompting push
# into a silent allow. Full reasoning on the `review_state` copy.
_GIT_ENV_UNSET = frozenset(
    {
        # Repository LOCATION. MEASURED 2026-09-19: these three each redirect a
        # gate decision on their own (branch, staged-diff hash, worktree key).
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        # Same documented class. MEASURED to have no effect in a standalone-repo
        # configuration — which is NOT "proven irrelevant": GIT_COMMON_DIR inside
        # a LINKED WORKTREE is the obvious untested case. Kept because adding to
        # a scrub is the safe direction.
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        # NOT location variables — they change what a diff REPORTS, which lands
        # in the same place. MEASURED 2026-09-19: `GIT_EXTERNAL_DIFF=/bin/true`
        # empties `git diff --cached`, so `review_state._staged_content_hash`
        # returns its "clean" (nothing-staged) sentinel and
        # `bump_review_round` then returns the current round WITHOUT
        # advancing — the escalation cap stops counting. `GIT_DIFF_OPTS` was
        # measured INERT here and is carried as its documented sibling only; do
        # not cite it as measured.
        "GIT_EXTERNAL_DIFF",
        "GIT_DIFF_OPTS",
    }
)

#: NO git CONFIG channel is handled here — not the FILE sources
#: (GIT_CONFIG_GLOBAL / GIT_CONFIG_SYSTEM) and not the injection channels
#: (GIT_CONFIG_COUNT / GIT_CONFIG_PARAMETERS). All four are PROTECTED config,
#: the only place git reads `safe.directory` from, so removing any of them makes
#: git refuse under a uid mismatch — and for THIS builder specifically they also
#: carry `credential.helper`, `url.*.insteadOf` and `http.*`, which this env
#: feeds to `git ls-remote` and to `gh` in `zero_drop_worker`. A local-read
#: hardening must not quietly disarm a remote-auth path. The config routes
#: measured to move a gate decision are closed by command flags instead; see the
#: `review_state` copy for the measurements.


def scrubbed_git_env() -> dict[str, str]:
    """The ambient environment with git's repo-discovery overrides removed."""
    return {k: v for k, v in os.environ.items() if k not in _GIT_ENV_UNSET}


async def default_runner(argv: list[str], timeout: float) -> tuple[int, str, str]:
    """Run a git command, returning (rc, stdout, stderr). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrubbed_git_env(),
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
    if err := _refuse_empty("for-each-ref", branches, raw=out):
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
    unparsed = 0
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/heads/") and _FULL_SHA.match(sha):
            heads[ref[len("refs/heads/") :]] = sha
        elif line.strip() and not ref.startswith(("refs/tags/", "refs/pull/")):
            # The third sibling, and the last one to get this. Its two peers
            # have counted unreadable lines from the start; this one dropped
            # them silently, so a PARTIAL parse — most lines unreadable, a few
            # readable — left `heads` truthy, passed `_refuse_empty`, and was
            # accepted as a COMPLETE remote listing. The dropped branches then
            # read as absent from the remote, which forks their identity into
            # the wrong class and RESOLVES their `pushed_no_pr` rows.
            #
            # Tags and pull refs are named rather than swept into the counter:
            # `--heads` should exclude them, but a server that sends them anyway
            # is not a format change and must not freeze the class.
            unparsed += 1
    if unparsed:
        return {"error": f"ls-remote: {unparsed} unparseable ref line(s)"}
    # On top of the class-wide resolve that `_refuse_empty` describes, an empty
    # set fails a second way here: it reclassifies EVERY branch as never-pushed,
    # and class is part of a finding's identity, so it forks rows instead of
    # correcting them.
    if err := _refuse_empty("ls-remote", heads, raw=out):
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


async def count_unique_work_commits(
    root: str, exclude: str, include: str, *, runner: Runner | None = None
) -> int | None:
    """Commits in *include* but not *exclude* that hold work existing nowhere
    else. ``None`` if unknown.

    Routine catch-up merges are excluded, because a branch whose only local-only
    commits are merges of the base has diverged by ancestry while holding
    nothing unique, and flagging it would be noise on top of a real signal. A
    count of 0 means "diverged, but every distinct change is already on the
    remote", and the caller turns that into ``PUSH_BEHIND`` — i.e. SUPPRESSES
    the finding. That is why what counts as "unique" has to be exactly right.

    **A CONFLICT-RESOLVED merge is unique work, and excluding it UNDERCOUNTS.**
    An earlier version ran ``rev-list --count --no-merges`` and its docstring
    asserted that "merge commits carry no unique work". That is true of an
    auto-merge and FALSE of a resolution: the resolved tree exists in neither
    parent, so the change is real and lives only here (Codex P1, PR #1794).

    **Scoped honestly against the report's stronger claim.** Codex described the
    consequence as a SUPPRESSION — a branch whose only local-only commit is such
    a merge counts 0, is labelled ``PUSH_BEHIND``, and vanishes. That specific
    outcome could not be constructed: for the range to hold only a merge, BOTH
    its parents must already be on the remote, and merging an already-merged
    branch produces no conflict to resolve. Every range reachable here that
    contains a resolved merge also contains the merged-in commits, so the
    classification stays ``PUSH_DIVERGED``.
    What IS reachable, and what this fix is for, is the COUNT: ``local_only``
    is reported to a human as "commits that exist nowhere else", and it silently
    understated that number by one per resolved merge. A wrong number under a
    true classification is still a wrong number, and this subsystem's whole
    claim is that its counts can be checked.

    MEASURED on this repository when that was fixed: of 23 recent merge commits,
    **3 (13%) carry a non-empty combined diff** — content present in neither
    parent. Not a hypothetical, and more likely here than elsewhere because this
    repo cannot rebase (a history-rewriting publish is hard-blocked), so
    branches catch up by merging and every conflict resolved that way lands in a
    merge commit.

    The discriminator is git's own combined diff (``--cc``), which by
    construction shows only hunks differing from EVERY parent. Empty combined
    diff = the merge contributed nothing of its own; non-empty = it did.
    """
    if not (_is_sha(exclude) and _is_sha(include)):
        logger.warning("zero_drop count_unique_work_commits refused a non-SHA argument")
        return None
    run = runner or default_runner
    rng = f"{exclude}..{include}"

    rc, out, err = await run(
        _git(root, "rev-list", "--count", "--no-merges", rng),
        ANCESTRY_TIMEOUT_S,
    )
    if rc != 0:
        logger.debug("zero_drop rev-list failed (rc=%s): %s", rc, err.strip()[:200])
        return None
    try:
        total = int(out.strip())
    except ValueError:
        return None

    # NUL-separated records so a commit subject can never be confused for a
    # diff line, and `%x00%H` so each record starts with its object name.
    #
    # `log.showSignature` is neutralised deliberately. The predicate below asks
    # "is this record's body non-empty", so anything else git may print into
    # that body is indistinguishable from merge content — and in a repository
    # that signs its commits, this config makes git emit signature-verification
    # lines for every commit shown. Left ambient, every signed merge would read
    # as unique work. `-c` here rather than in `_git` because it is this one
    # reader's requirement, not a property every git call in the module needs.
    rc, out, err = await run(
        _git(
            root,
            "-c",
            "log.showSignature=false",
            # `diff.context` is PINNED because the predicate below reads hunk
            # GROUPING, and the context width is what decides where one hunk
            # ends and the next begins. MEASURED on git 2.43 over this
            # repository's 36 merges: the same command counts 6 / 7 / 8 merges
            # as carrying unique work at context 0 / 3 / 10. One constructed
            # merge makes the mechanism plain — two parents each adding a
            # different line a line apart, the merge keeping BOTH: at context 3
            # that is ONE hunk matching neither parent (shown, counted); at
            # context 0 it splits into two hunks that each match one parent,
            # both suppressed as uninteresting, and the merge vanishes.
            # Git's default is 3; the value only has to be STABLE, not special.
            # What must not happen is a repo-level or user-level setting
            # silently retuning a number this detector publishes.
            "-c",
            "diff.context=3",
            "log",
            "--merges",
            "--cc",
            "--format=%x00%H",
            rng,
        ),
        ANCESTRY_TIMEOUT_S,
    )
    if rc != 0:
        # The merge leg is the half that PREVENTS a suppression, so failing it
        # must not silently fall back to the old, wrong number. Unknown, not 0.
        logger.debug("zero_drop merge-diff scan failed (rc=%s): %s", rc, err.strip()[:200])
        return None

    for record in out.split("\0")[1:]:
        # Split at the FIRST NEWLINE rather than a fixed object-name width. The
        # width is the repository's hash format — 40 hex for SHA-1, 64 for
        # SHA-256 — so a hardcoded slice leaves object-name digits in the body
        # of a SHA-256 repository, where they read as content.
        _, _, body = record.partition("\n")
        # `--cc` suppresses a hunk whose "contents in the parents have only two
        # variants and the merge result picks one of them without modification"
        # (git-diff-tree(1), verbatim). For a two-parent merge that is exactly
        # "the result matched NEITHER parent here", so a non-empty body IS the
        # definition of "this merge contributed something of its own".
        #
        # The property is HUNK-level, not LINE-level, and the difference is a
        # trap worth naming because a reviewer fell into it. A merge that keeps
        # BOTH parents' additions has a hunk matching neither parent — real
        # content living only on this branch — while EVERY LINE in it came from
        # one parent or the other, so no line carries a mark in both combined-
        # diff columns. MEASURED on this repository: 5 of the 7 merges counted
        # here are that shape. Testing the columns instead would silently stop
        # counting them, which is the SUPPRESSING direction.
        #
        # The one shape this over-counts is an OCTOPUS merge, where "only two
        # variants" can fail with three or more parents, so a hunk may be shown
        # that does match one parent. Over-count is the safe direction for a
        # detector and octopus merges are vanishingly rare here (0 of 36).
        #
        # Ask the question directly rather than inspecting the
        # body's shape: the previous form scanned for a line starting `+` or
        # `-` and therefore missed a hand-resolved BINARY conflict, whose
        # entire combined diff is the line `Binary files differ` (MEASURED on
        # git 2.43). The branch was then counted at zero and suppressed as
        # PUSH_BEHIND — the false-clean this function exists to prevent,
        # arriving through the one door the text-conflict fix left open.
        if body.strip():
            total += 1
    return total


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
    # `-z` for the same reason `status` uses it: a worktree path may legally
    # contain a NEWLINE, and the line-oriented porcelain would then split one
    # record into two — yielding a truncated path that resolves to nothing and a
    # phantom remainder. VERIFIED against git 2.43: `-z` NUL-TERMINATES each
    # attribute and separates records with an empty field, so the branch tests
    # below are unchanged and an empty field falls through all of them without
    # counting as unparsed (Codex P2, PR #1794).
    rc, out, err = await run(
        _git(root, "worktree", "list", "--porcelain", "-z"), REF_SWEEP_TIMEOUT_S
    )
    if rc != 0:
        return {"error": f"worktree list failed (rc={rc}): {err.strip()[:300]}"}
    worktrees: list[dict] = []
    current: dict = {}
    unparsed = 0
    for line in out.split("\0"):
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
    if not worktrees:
        # NOT `_refuse_empty`, and the difference is the whole point of that
        # helper's split. For the two ref enumerators an empty OUTPUT is a true
        # observation — an unborn repo, a cleared remote — so they accept it.
        # Here it cannot be: `git worktree list` always names the main worktree
        # of any repository it can read at all, so nothing parsed means the
        # output is not what we think it is, whether it was empty or garbage.
        # Stated locally rather than borrowed, because the reason is this
        # command's, not the shared guard's.
        return {
            "error": (
                "worktree list returned no records (rc=0) — the main worktree is "
                "always listed, so an empty parse means the output is unreadable"
            )
        }
    # Stamped HERE, over the whole listing, because it is a fact about the
    # listing rather than a judgement about any one worktree — and because the
    # two consumers see different subsets of it. `git worktree add --force`
    # checks out a branch already checked out elsewhere, and the classifier
    # keys a finding on the branch, so two such worktrees would collapse onto
    # one identity and one of them could never be acknowledged. Computing the
    # duplicate set at each consumer instead would let the worker's HOLD path
    # (which sees prunable and unreadable worktrees) and the classifier (which
    # does not) disagree about which branches are duplicated — a hold key that
    # matches no finding, which fails silently in the resolve direction.
    #
    # The cost of one population, stated rather than left to be found: a
    # PRUNABLE registration still counts, so a live worktree can be
    # discriminated because of a sibling whose directory no longer exists, and
    # un-discriminated again once that registration is pruned. Both transitions
    # are visible ones — the row re-opens immediately — and the alternative,
    # two populations that must agree, fails silently instead.
    seen: dict[str, int] = {}
    for wt in worktrees:
        if wt["branch"]:
            seen[wt["branch"]] = seen.get(wt["branch"], 0) + 1
    for wt in worktrees:
        wt["branch_duplicated"] = bool(wt["branch"]) and seen[wt["branch"]] > 1
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

    **``--untracked-files=all`` is load-bearing, not a default made explicit.**
    Under git's default (``normal``) an untracked DIRECTORY collapses to a
    single ``?? dir/`` entry, so nothing about its contents reaches the caller.
    The dirty-state key is derived from these entries, and an ack is keyed to
    that state — so adding a whole new file inside an already-untracked
    directory left the key BYTE-IDENTICAL and the acknowledgement went on
    suppressing work it was never granted against (Codex P1, PR #1794).
    DEMONSTRATED: with two files under an untracked `somedir/`, default mode
    printed `?? somedir/` before and after editing a child AND adding a third
    file; `-uall` listed each child and gained the new one.
    It also makes the sentence above true — under the default, "an untracked
    source file" inside a new directory was exactly what could NOT be seen.
    Cost MEASURED across 14 real worktrees on this install: identical entry
    counts either way (delta 0), because the build/temp output is gitignored. A
    clone where it is not is bounded by ``STATUS_TIMEOUT_S``, and a timeout
    degrades the class to HELD rather than clean.
    """
    run = runner or default_runner
    rc, out, err = await run(
        _git(path, "status", "--porcelain", "-z", "--untracked-files=all"),
        STATUS_TIMEOUT_S,
    )
    if rc != 0:
        return {"error": f"status failed (rc={rc}): {err.strip()[:200]}"}
    entries, unparsed = parse_status_z(out)
    if unparsed:
        # A successful CALL with unreadable OUTPUT is still a failed read. The
        # rc check above only catches the former; without this, a garbled
        # porcelain stream reported a CLEAN worktree.
        return {"error": f"status: {unparsed} unparseable porcelain record(s)"}
    return {"entries": entries}
