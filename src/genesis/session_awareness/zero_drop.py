"""Zero-drop classification — the pure core of the stranded-work detector.

Nothing here does I/O. The git and gh atoms hand this module their results and
it decides which conditions are STRANDED, which are covered, and which are
suppressed-but-counted. Keeping it pure is what makes the acceptance replay
possible: the two known-stranded corpus branches are classified by the same
function the live worker calls, from recorded inputs.

The classification problem, stated honestly (MEASURED on this install
2026-09-05, 1651 PRs / 209 refs): a squash-merging repo (``mergeCommitAllowed:
false``) never makes a merged branch tip an ancestor of ``origin/main``, so
EVERY merged branch reads permanently "ahead". A naive ahead-count query
returned 145 candidates of which only ~18 were real — ~12% precision. Four
name-free git signals were tried and all four failed, which is why a join on
PR history by head-ref NAME is here at all.

**The name join is evidence about the pipeline, never proof the work landed.**
That distinction is the whole design, and getting it wrong was this module's
first real defect: a cross-model review found that "a PR with this name merged
after your tip date" suppressed branches holding commits the PR never
contained. MEASURED 2026-09-06 across 217 refs — 5 branches carrying commits
that exist on no remote were suppressed, 4 of them by CLOSED PRs and 1 by an
OPEN one. A clean board that hides work is the exact failure this detector
exists to prevent, so verdicts are now ordered by EVIDENCE STRENGTH:

1. **SHA proof.** ``headRefOid == tip`` means the PR contained exactly this
   commit. MEASURED: 119 of 123 merged-covered branches match exactly.
2. **Ancestry.** The tip is reachable from the PR's head, so everything local
   was in the PR. Costs one local ``merge-base`` and needs no clocks.
3. **Push state.** ``ls-remote`` gives the remote's tip SHA. If the local tip
   IS that SHA, nothing here exists only on this machine, whatever the PRs
   say. If it differs and the tip is not merely behind, local-only commits are
   PROVEN and no PR on that ref can cover them.
4. **Time guards** (``mergedAt``/``closedAt`` vs the tip date) — kept, but
   demoted to confirming a tip we already know is pushed. Head-ref names get
   reused (MEASURED: 35 of 1586 names, one carrying 7 PRs).
5. **The name join itself** — indexing only, and scoped to head refs in our
   own repository so a fork PR cannot cover a same-named local branch.

Where none of that settles it — the merged PR's head SHA is not in this
object store, so ancestry is unanswerable — the branch is FLAGGED with the
reason recorded and a one-command hint for resolving it by hand. Suppressing
on evidence we could not collect is the failure mode; an extra acknowledged
row is not.

Everything the run suppresses is reported as a stage count with its
denominator. There are no prefix denylists by owner decision: a backup or
scratch branch that flags is acknowledged with a reason (and because such a
branch never moves, the SHA-keyed ack never expires), which leaves a record of
the judgement instead of a rule nobody can see.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

# The alert's row grammar, defused. Git ref names may legally contain `|` and
# `·` (check-ref-format bans control characters, space, and `~^:?*[\` — but not
# these), and a branch name is content this process did not author. A worktree
# path may contain anything at all. Substituted rather than deleted so the text
# stays readable.
_ALERT_GRAMMAR_CHARS = str.maketrans({"|": "/", "·": "-", "[": "(", "]": ")"})
_WHITESPACE_RUN = re.compile(r"\s+")

# Characters that RENDER as nothing, or reorder what surrounds them. `\s+`
# above collapses ASCII whitespace and Unicode separators; it does not touch
# the Cf (format) category — bidi overrides U+202A-U+202E and isolates
# U+2066-U+2069, zero-width joiners, and friends. A filesystem path may legally
# contain any of them (Linux forbids only NUL and '/'), so a worktree path can
# carry text that displays to a human or a model as something other than what
# it is. Deleted rather than substituted: unlike `|` and `[`, these have no
# readable form to preserve.
_INVISIBLE_CHARS = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u206f\ufeff]")


def neutralise(value: str | None) -> str | None:
    """Flatten untrusted text and defuse the alert's row grammar. NO bound.

    Branch names, worktree paths and the diagnostic blobs built from them reach
    a MODEL — through the observations the worker writes AND through the
    ``zero_drop_status`` MCP response. Flattening newlines and substituting the
    grammar characters stops chosen text from forging an extra row, or an extra
    field inside one, that a reader would attribute to the detector itself.

    Lives in the pure module because BOTH those surfaces need it and only one
    of them had it. A sanitiser every writer must remember to call is a
    convention, and a convention is what a reviewer finds one missing instance
    of at a time; one importable function is a chokepoint.

    ``None`` passes through as ``None``: the callers distinguish "no value" from
    "an empty one", and a nullable column rendered as ``""`` reads as a
    worktree path that exists and is blank.

    Deliberately separate from any bound: the renderers budget differently (one
    identity vs a diagnostic blob), and folding a bound in meant the second
    caller either reused a limit written for something else or skipped the
    sanitising entirely. It skipped it.
    """
    if value is None:
        return None
    flattened = _INVISIBLE_CHARS.sub("", str(value).translate(_ALERT_GRAMMAR_CHARS))
    return _WHITESPACE_RUN.sub(" ", flattened).strip()


CLASS_UNPUSHED = "unpushed_branch"
CLASS_PUSHED_NO_PR = "pushed_no_pr"
CLASS_DIRTY = "dirty_worktree"

# A detached worktree has no branch to key on, so its identity is its path.
# ':' is forbidden in a git ref name (check-ref-format), so this prefix can
# never collide with a real branch identity.
DETACHED_KEY_PREFIX = "@detached:"


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a timestamp to an AWARE datetime, or None.

    Aware is not a nicety here. `now` and the age cutoffs are timezone-aware,
    and comparing an aware datetime with a naive one raises TypeError rather
    than returning a wrong answer — which would escape `classify_branches`,
    escape the branch leg, and be caught only by the worker's outer handler as
    a failed sweep. git's `%(committerdate:iso-strict)` always carries an
    offset, but `mergedAt` comes from gh and `tip_date` can be replayed from a
    recorded fixture, so the input is not ours to assume. A naive value is
    read as UTC, which is what every producer here means.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# A local branch's relationship to the remote ref of the same name. This is
# evidence tier 3 and it is computed from SHAs, so it does not depend on PR
# names or on any clock.
PUSH_EXACT = "exact"  # local tip IS the remote tip: nothing is local-only
PUSH_BEHIND = "behind"  # remote has moved on; every local commit is pushed
PUSH_DIVERGED = "diverged"  # PROVEN local-only commits (non-merge, so real work)
PUSH_ABSENT = "absent"  # no remote branch of this name (never pushed, or deleted)
PUSH_UNKNOWN = "unknown"  # differs, but ancestry was unanswerable


def index_prs_by_head(prs: list[dict], *, owner: str | None = None) -> tuple[dict, int]:
    """Group PR records by ``headRefName``. Returns ``(index, ignored_forks)``.

    Rows without a head-ref name are dropped. When *owner* is given, PRs whose
    head branch lives in a DIFFERENT account are excluded from the join and
    counted: a contributor's fork branch named ``patch-1`` says nothing about a
    local ``patch-1``, and head-ref name reuse is already MEASURED at 35 of
    1586 names here. Excluding them is cheap insurance — MEASURED 2026-09-06,
    9 of 1665 PRs come from forks and none currently collides with a local
    branch name, so this closes a real hole at zero present cost.

    ``owner=None`` keeps every PR, for a caller that could not resolve the
    repository owner. That is the safe direction: an over-broad join can only
    SUPPRESS, and a suppression here is visible in the stage counts, whereas
    dropping every PR would flag the entire branch list at once.
    """
    index: dict[str, list[dict]] = {}
    ignored = 0
    for pr in prs:
        head = pr.get("headRefName")
        if not (isinstance(head, str) and head):
            continue
        if owner is not None:
            pr_owner = pr.get("headRepositoryOwnerLogin")
            if isinstance(pr_owner, str) and pr_owner.lower() != owner.lower():
                ignored += 1
                continue
        index.setdefault(head, []).append(pr)
    return index, ignored


def pr_coverage(
    prs_for_branch: list[dict],
    *,
    tip_date: datetime | None,
    tip_sha: str | None = None,
    push_state: str = PUSH_UNKNOWN,
    ancestry: dict | None = None,
) -> tuple[str, dict]:
    """Does any PR on this head ref COVER the local tip?

    Returns ``(verdict, evidence)``. Verdicts, strongest evidence first:

    ``merged``            a PR merged this exact commit, or a commit that
                          contains it — SHA proof or ancestry, no clock.
    ``open``              an open PR is reviewing the pushed tip.
    ``closed``            a PR for this ref was deliberately abandoned AFTER
                          the local tip existed, and the tip is not local-only.
    ``merged_local_only`` a PR merged, and this tip provably was NOT in it.
    ``merge_unconfirmable`` a PR merged from a head we cannot compare against
                          (its object is not in this repository), so coverage
                          is unproven. FLAGGED, not suppressed.
    ``merged_predates_tip`` / ``merged_tip_undated`` the older time-guard
                          shapes, kept: a merge that cannot vouch for this tip.
    ``closed_local_only`` / ``closed_tip_postdates`` the CLOSED equivalents.
    ``none``              no PR on this ref at all.

    *ancestry* maps ``"<a>..<b>"`` to True/False/None — "is a reachable from
    b" — computed by the caller (this module does no I/O). ``None`` means the
    object was not in the repository, which is why ``merge_unconfirmable``
    exists as a verdict rather than being folded into either answer.

    The evidence dict is small and structural on purpose: PR number, URL, the
    timestamps that drove the verdict. It reaches a model through the findings
    store, so it carries no PR prose and no account names.
    """
    ancestry = ancestry or {}
    # TWO predicates, not one and its negation — and that distinction is a
    # defect this file already made once. A five-valued push state does not
    # reduce to a boolean: `not local_only` is NOT "the tip is on the server",
    # it is "the tip is not PROVABLY off it", and ABSENT sits in that gap
    # (MEASURED 2026-09-06: 159 of 217 refs). Using the negation as a licence to
    # suppress put the majority state on the permissive side of three separate
    # branches below.
    #
    # `local_only` — PROVEN off the server. `diverged` means the remote ref of
    # this name exists at a different SHA and the local tip is not merely behind
    # it: a fact about commits, not about names. ABSENT does NOT qualify, though
    # the temptation is strong — a branch missing from the remote is usually one
    # that merged and was deleted, its commits still reachable via
    # refs/pull/<n>/head, so treating absence as proof would flag most of the
    # repository.
    #
    # `proven_pushed` — PROVEN on it. Only these two states let a PR's TIMESTAMP
    # stand in for evidence about the commits, because a clock can only confirm
    # a tip we already know the server has.
    local_only = push_state == PUSH_DIVERGED
    proven_pushed = push_state in (PUSH_EXACT, PUSH_BEHIND)

    def _contains(head: str | None) -> bool | None:
        """Did the PR's merged head contain our tip? None = unanswerable."""
        if not (head and tip_sha):
            return None
        if head == tip_sha:
            return True
        return ancestry.get(f"{tip_sha}..{head}")

    merged_predates = False
    merged_undatable_tip = False
    merged_unconfirmable: dict | None = None
    merged_disproven: dict | None = None
    open_not_covering: dict | None = None
    closed_rows: list[dict] = []

    for pr in prs_for_branch:
        state = (pr.get("state") or "").upper()
        head = pr.get("headRefOid")
        if state == "OPEN":
            # An open PR reviews what is ON THE REMOTE, so it covers this tip
            # only when the tip is known to BE there. `not local_only` was the
            # wrong test: it also passed ABSENT, where no ref of this name is on
            # the remote at all, letting a name-level fact suppress a branch
            # against the SHA-level evidence this ordering exists to prefer.
            # Keep scanning either way — a MERGED row for the same ref can still
            # carry SHA proof, which outranks an open PR.
            # MEASURED 2026-09-06: 0 of 221 refs are ABSENT with an open PR, so
            # this tightening changes no current row.
            if not proven_pushed:
                open_not_covering = {"pr": pr.get("number"), "url": pr.get("url")}
                continue
            return "open", {"pr": pr.get("number"), "url": pr.get("url")}
        if state == "MERGED":
            contained = _contains(head)
            if contained is True:
                return "merged", {
                    "pr": pr.get("number"),
                    "proof": "head_oid" if head == tip_sha else "ancestor_of_merged_head",
                    "merged_head": head,
                }
            merged_at = _parse_iso(pr.get("mergedAt"))
            if merged_at is None:
                continue  # a MERGED row with no mergedAt proves nothing
            if contained is None and head:
                # The merged head is not an object we hold, so we cannot test
                # whether it contained this tip. Recorded with the hint that
                # resolves it: GitHub keeps refs/pull/<n>/head forever.
                merged_unconfirmable = {
                    "pr": pr.get("number"),
                    "merged_head": head,
                    "merged_at": pr.get("mergedAt"),
                }
                if tip_sha and pr.get("number"):
                    # Only emit a hint that can actually be RUN. Built
                    # unconditionally it rendered `--is-ancestor None
                    # FETCH_HEAD` whenever the tip was unknown — a command that
                    # fails in a way pointing at the reader's shell rather than
                    # at the missing evidence, which is worse than no hint.
                    merged_unconfirmable["resolve_with"] = (
                        f"git fetch origin refs/pull/{pr.get('number')}/head && "
                        f"git merge-base --is-ancestor {tip_sha} FETCH_HEAD"
                    )
                continue
            if contained is False:
                merged_disproven = {
                    "pr": pr.get("number"),
                    "merged_head": head,
                    "merged_at": pr.get("mergedAt"),
                }
                continue
            # No head SHA on the row at all: fall back to the time guard, which
            # can only vouch for a tip we already know is on the remote.
            if tip_date is None:
                merged_undatable_tip = True
            elif merged_at >= tip_date and proven_pushed:
                return "merged", {
                    "pr": pr.get("number"),
                    "proof": "merged_after_tip",
                    "merged_at": pr.get("mergedAt"),
                }
            else:
                merged_predates = True
        elif state == "CLOSED":
            closed_rows.append(pr)

    # Nothing below this line suppresses — every branch of it FLAGS — so the
    # ordering is not about strength of evidence any more, it is about where
    # the reader should look first. One rule: name the LIVE pull request. An
    # open PR that does not contain your commits is fixed by a push; a merge
    # that predates them, or a closed PR that never saw them, is history.
    if open_not_covering:
        # Distinct from `none` on purpose — "your PR does not contain your
        # local commits" is a different disposition from "this branch has no
        # PR", and a label that says the wrong one wastes the reader's first
        # move.
        return "local_ahead_of_open_pr", open_not_covering
    # Merged-but-disproven outranks the unconfirmable case: one says the work
    # was NOT in the PR, the other says we could not tell.
    if merged_disproven:
        return "merged_local_only", merged_disproven
    if merged_unconfirmable:
        return "merge_unconfirmable", merged_unconfirmable
    if merged_predates:
        return "merged_predates_tip", {}
    if merged_undatable_tip:
        return "merged_tip_undated", {}

    if closed_rows:
        return _closed_verdict(
            closed_rows,
            tip_date=tip_date,
            local_only=local_only,
            proven_pushed=proven_pushed,
            contains=_contains,
        )
    return "none", {}


def _closed_verdict(
    closed_rows: list[dict],
    *,
    tip_date: datetime | None,
    local_only: bool,
    proven_pushed: bool,
    contains,
) -> tuple[str, dict]:
    """Does a CLOSED (abandoned) PR account for this branch's local tip?

    Closing a PR is a decision about the content that was IN it, so the first
    question is what it contained — the same question, answered by the same
    evidence, as the merged path. An earlier version asked only about push
    state and clocks here, while the worker was already resolving ancestry for
    these very rows and throwing the answer away.

    MEASURED 2026-09-06 on 14 branches whose only coverage is a closed PR, and
    it settles a design argument: 8 are `exact` and PROVEN contained, 4 are
    `diverged` and PROVEN not, and **2 are ABSENT from the remote yet still
    PROVEN contained**. Deciding by push state alone would have flagged those
    two — so SHA evidence is not merely stronger here, it is the difference
    between 4 findings and 6, two of which would be wrong.

    Order, therefore: proof of containment suppresses; proof of absence flags;
    only when the commits settle nothing does push state and then the clock
    get a say. Commits made AFTER the close are not covered either — the
    MERGED verdict has had that guard from the start and its absence here was
    an asymmetry justified nowhere. A genuinely abandoned branch is still
    suppressed, and one that flags takes a single acknowledgement that never
    expires, because a dead branch never moves.
    """
    unanswerable = False
    for pr in closed_rows:
        verdict = contains(pr.get("headRefOid"))
        if verdict is True:
            return "closed", {"pr": pr.get("number"), "proof": "head_oid_or_ancestor"}
        if verdict is False:
            return "closed_local_only", {
                "pr": pr.get("number"),
                "proof": "not_an_ancestor_of_the_closed_head",
            }
        unanswerable = True

    # Same verdict, DIFFERENT evidence, and the difference is the point: one of
    # these is proven and the other is merely unrefuted. `local_only` implies
    # `not proven_pushed`, so without distinct labels the first branch would be
    # dead code wearing the second's clothes — and a reader could not tell
    # "these commits are provably on no remote" from "we could not check".
    if local_only:
        return "closed_local_only", {
            "pr": closed_rows[0].get("number"),
            "proof": "diverged_from_remote",
        }
    if unanswerable and not proven_pushed:
        # No SHA answer, and the tip is not known to be on the server either.
        # Suppressing here would rest on the branch NAME and a timestamp, which
        # is the evidence tier this module exists to stop trusting.
        return "closed_local_only", {"pr": closed_rows[0].get("number"), "proof": "unconfirmed"}
    latest = None
    for pr in closed_rows:
        closed_at = _parse_iso(pr.get("closedAt"))
        # A CLOSED row with no closedAt proves nothing — the exact mirror of
        # the MERGED rule above, so an undated close falls through to flagging
        # rather than suppressing on a field we did not get.
        if closed_at is not None and (latest is None or closed_at > latest):
            latest = closed_at
    if latest is None or tip_date is None or tip_date > latest:
        return "closed_tip_postdates", {}
    return "closed", {}


def classify_branches(
    branches: list[dict],
    *,
    push_states: dict[str, str],
    prs: list[dict],
    now: datetime,
    min_age_hours: int = 12,
    repo_owner: str | None = None,
    ancestry: dict | None = None,
) -> dict:
    """Split every local branch into findings + a full stage accounting.

    Returns ``{"findings": {class: [finding, ...]}, "stages": {...}}``. Every
    branch is counted in exactly one terminal stage, so the stage counts sum
    to the ref total — suppression you cannot add up is suppression you cannot
    audit.

    ``push_states`` maps branch -> one of the ``PUSH_*`` constants, computed by
    the caller (this module does no I/O). ``ancestry`` maps ``"<a>..<b>"`` to
    True/False/None for the pairs the caller resolved.

    Two kinds of "we could not tell" are HELD rather than reported, and the
    distinction between them and "the condition ended" is the bug this
    classifier keeps almost making. Reporting stranded work on evidence we
    failed to collect teaches everyone to ignore the board; RESOLVING an
    existing finding on that same missing evidence quietly clears it. Both
    directions are wrong, and only the first is obvious.

    - ``ahead_unknown`` — an old git, or a broken base ref.
    - ``push_unknown`` — the local tip differs from the remote tip, but the
      remote object is not in this repository so we cannot tell whether the
      branch is genuinely ahead or merely behind. MEASURED 2026-09-06: 0 of
      217 refs here, because a branch pushed from this machine keeps its
      objects — but a repo cloned after the push would land here.
    """
    index, ignored_forks = index_prs_by_head(prs, owner=repo_owner)
    cutoff = now - timedelta(hours=min_age_hours)
    findings: dict[str, list[dict]] = {CLASS_UNPUSHED: [], CLASS_PUSHED_NO_PR: []}
    held: set[str] = set()
    # TERMINAL stages only: every branch lands in exactly one of these, so they
    # sum to refs_total and the suppression stays auditable. Metadata counts
    # (fork PRs ignored, held totals) are added by the CALLER outside this
    # dict — putting a PR-level count in here would break the sum invariant on
    # every run that saw a fork PR.
    stages = dict.fromkeys(
        (
            "refs_total",
            "ahead_unknown",
            "push_unknown",
            "not_ahead",
            "too_young",
            "covered_open_pr",
            "covered_merged_pr",
            "suppressed_closed_pr",
            "flagged_merge_predates_tip",
            "flagged_merged_tip_undated",
            "flagged_merged_local_only",
            "flagged_merge_unconfirmable",
            "flagged_closed_local_only",
            "flagged_closed_tip_postdates",
            "flagged_local_ahead_of_open_pr",
            "flagged_no_pr",
        ),
        0,
    )
    stages["refs_total"] = len(branches)

    # Every `continue` below is one of two KINDS, and conflating them is the
    # bug this classifier keeps almost making:
    #   "the condition genuinely ended"  -> absent from `present`, so the
    #       reconciler resolves the finding. Correct for not_ahead and for the
    #       PR-coverage verdicts.
    #   "we could not determine"         -> HELD. Never resolve a finding on
    #       evidence we failed to collect.
    for row in branches:
        branch = row["branch"]
        ahead = row.get("ahead")
        if ahead is None:
            # An old git expands %(ahead-behind:) empty, and a broken base ref
            # yields nothing. We do NOT know this branch is clean — we failed to
            # measure it — so it is held, not resolved. (Missed on the first
            # pass, which held age-gated branches and let this one through: the
            # identical mistake one branch over.)
            stages["ahead_unknown"] += 1
            held.add(branch)
            continue
        if ahead <= 0:
            # Genuinely no longer ahead of the base: the condition ended.
            stages["not_ahead"] += 1
            continue
        tip_date = _parse_iso(row.get("tip_date"))
        if tip_date is not None and tip_date > cutoff:
            # Work in flight right now is not stranded work. An UNDATED tip
            # (unparseable) is judged on its merits rather than excused.
            # HELD, not absent: a branch under the age gate is one we looked at
            # and chose not to report, so it must not resolve an existing row.
            stages["too_young"] += 1
            held.add(branch)
            continue

        push_state = push_states.get(branch, PUSH_UNKNOWN)
        if push_state == PUSH_UNKNOWN:
            # The tip differs from the remote ref of this name and ancestry was
            # unanswerable, so we cannot tell "ahead" from "behind". HELD for
            # the same reason ahead_unknown is: a guess in either direction is
            # a claim we cannot support, and the wrong one is silent.
            stages["push_unknown"] += 1
            held.add(branch)
            continue

        verdict, evidence = pr_coverage(
            index.get(branch, []),
            tip_date=tip_date,
            tip_sha=row.get("tip_sha"),
            push_state=push_state,
            ancestry=ancestry,
        )
        if verdict == "open":
            stages["covered_open_pr"] += 1
            continue
        if verdict == "merged":
            stages["covered_merged_pr"] += 1
            continue
        if verdict == "closed":
            stages["suppressed_closed_pr"] += 1
            continue

        stage_for = {
            "merged_predates_tip": "flagged_merge_predates_tip",
            "merged_tip_undated": "flagged_merged_tip_undated",
            "merged_local_only": "flagged_merged_local_only",
            "merge_unconfirmable": "flagged_merge_unconfirmable",
            "closed_local_only": "flagged_closed_local_only",
            "closed_tip_postdates": "flagged_closed_tip_postdates",
            "local_ahead_of_open_pr": "flagged_local_ahead_of_open_pr",
        }
        if verdict in stage_for:
            stages[stage_for[verdict]] += 1
        else:
            stages["flagged_no_pr"] += 1

        # CLASS = is this tip on the server? `pushed_no_pr` means "the work is
        # safe, but it is in no pipeline"; `unpushed_branch` means "these
        # commits exist only here". A DIVERGED branch belongs to the second
        # even though a ref of its name is on the remote — the class describes
        # the commits, not the name. Class is part of the finding's identity,
        # so this decides which row a branch reopens.
        pushed = push_state in (PUSH_EXACT, PUSH_BEHIND)
        findings[CLASS_PUSHED_NO_PR if pushed else CLASS_UNPUSHED].append(
            {
                "branch": branch,
                "tip_sha": row.get("tip_sha"),
                "ahead_count": ahead,
                "details": {
                    "reason": verdict,
                    "behind": row.get("behind"),
                    "tip_date": row.get("tip_date"),
                    "pushed": pushed,
                    "push_state": push_state,
                    **({"local_only_commits": row["local_only"]} if row.get("local_only") else {}),
                    **({"evidence": evidence} if evidence else {}),
                },
            }
        )

    return {
        "findings": findings,
        "stages": stages,
        "held": held,
        "ignored_forks": ignored_forks,
    }


def dirty_state_key(entries: list[tuple[str, str]], newest: datetime | None) -> str:
    """The EXPIRY key for a dirty-worktree finding — its branch-tip analogue.

    An acknowledgement is keyed to the state it was granted against and expires
    the moment that state changes. A branch has a tip SHA for this; a dirty
    worktree has nothing equivalent, and leaving the key empty does not fail
    loudly — it makes the ack PERMANENT, because the expiry test compares
    ``acked_tip_sha != tip_sha`` and ``None != None`` is False. So an
    acknowledged worktree would stay suppressed through every later edit, which
    is precisely the "mute this forever" the ack design refuses to offer.

    Digest of what "the work changed" means for a worktree: which paths are
    dirty, how each is dirty, and when it last changed. Add, remove, or touch a
    file and the key moves. The full digest is stored — a key is the one thing
    that must never be shortened, since two states colliding on a prefix would
    silently transfer one worktree's acknowledgement to another's work.
    """
    import hashlib

    payload = "\n".join(sorted(f"{xy}\t{path}" for xy, path in entries))
    payload += f"\n@{newest.isoformat() if newest else 'undated'}"
    return hashlib.sha256(payload.encode()).hexdigest()


# An identity is returned VERBATIM (it is the ack key), so the refusal here has
# to cover everything the display sanitiser would otherwise have removed —
# control characters AND the invisible/reordering ones. A key cannot be cleaned
# without merging identities, so the only safe answer for a hostile one is to
# refuse it.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _safe_identity(value: str | None) -> bool:
    """Can this identity be stored as a key AND emitted verbatim to a model?

    An identity is the one field that must survive a round trip unchanged —
    callers read it from ``zero_drop_status`` and pass it to ``zero_drop_ack``
    — so it is the one field a sanitiser must not touch. That leaves refusal as
    the only way to keep it safe to emit, which is what this is: a value
    carrying a control character never becomes an identity.

    Branch identities are safe by git's own rules (check-ref-format bans
    control characters). Path-keyed detached identities are not, which is the
    whole reason this exists.
    """
    return bool(value) and not (_CONTROL_CHARS.search(value) or _INVISIBLE_CHARS.search(value))


def worktree_identity(observation: dict) -> str:
    """The stable identity of a worktree finding.

    The branch name when there is one — a worktree is one-to-one with its
    branch, and the path can change while the work does not. A DETACHED
    worktree has no branch, so it keys on its path behind a prefix containing
    ':', which git forbids in a ref name, so the two spaces cannot collide.

    Shared rather than inlined because the HOLD set and the finding row must
    agree on it exactly: an identity computed one way in one place and another
    way in the other would hold a key nothing matches, silently restoring the
    resolve-on-absence behaviour the hold exists to prevent.
    """
    branch = observation.get("branch")
    return branch or f"{DETACHED_KEY_PREFIX}{observation['path']}"


def classify_worktrees(observations: list[dict], *, now: datetime, min_age_hours: int = 6) -> dict:
    """Findings for worktrees carrying uncommitted work.

    ``observations`` is one dict per worktree: ``{path, branch, detached,
    entries, newest_mtime}`` where ``entries`` is the parsed status output and
    ``newest_mtime`` the most recent modification time among the dirty paths
    (None when nothing could be stat'd). The age gate reads that mtime, NOT
    the branch tip: a worktree with a months-old tip and a two-minute-old edit
    is somebody typing, not stranded work.
    """
    cutoff = now - timedelta(hours=min_age_hours)
    findings: list[dict] = []
    held: set[str] = set()
    stages = dict.fromkeys(
        ("worktrees_total", "clean", "too_young", "quarantined_identity", "flagged_dirty"), 0
    )
    stages["worktrees_total"] = len(observations)

    for obs in observations:
        if not _safe_identity(worktree_identity(obs)):
            # The identity is the ACK KEY and it round-trips verbatim through
            # the MCP surface, so it is the one field that cannot be sanitised
            # without merging two identities onto one key. A detached worktree
            # keys on its PATH, and a path — unlike a git ref name — may
            # contain newlines and escape sequences. Refusing such a value is
            # the resolution: it never becomes a key, so the key stays safe to
            # emit whole, and the refusal is COUNTED rather than silent.
            # MEASURED 2026-09-06: 0 of 161 worktrees here.
            stages["quarantined_identity"] += 1
            continue
        entries = obs.get("entries") or []
        if not entries:
            stages["clean"] += 1
            continue
        newest = obs.get("newest_mtime")
        if newest is not None and newest > cutoff:
            # HELD, not absent — and this is the case that made the distinction
            # matter. One edit inside a worktree moves newest_mtime, so an
            # acknowledged worktree drops under the gate for a single sweep;
            # treating that as "gone" resolved the row and destroyed a written
            # acknowledgement that ordinary typing had no business revoking.
            stages["too_young"] += 1
            held.add(worktree_identity(obs))
            continue
        stages["flagged_dirty"] += 1
        tracked = sum(1 for xy, _ in entries if xy != "??")
        untracked = len(entries) - tracked
        findings.append(
            {
                "branch": worktree_identity(obs),
                "tip_sha": dirty_state_key(entries, newest),
                "worktree_path": obs["path"],
                "details": {
                    "tracked_changes": tracked,
                    "untracked_files": untracked,
                    "newest_change_at": newest.isoformat() if newest else None,
                    "detached": bool(obs.get("detached")),
                },
            }
        )

    return {"findings": findings, "stages": stages, "held": held}
