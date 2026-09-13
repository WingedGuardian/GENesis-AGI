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
   PR history is therefore indexed BOTH ways — by head-ref name and by head
   SHA — because a name-only lookup gates tier 1 behind tier 5: a branch
   renamed (or checked out locally under another name) matches no historical
   ``headRefName``, so the exact-SHA evidence never reached the classifier at
   all. MEASURED 2026-09-12 over 251 refs / 1775 PRs: 4 of 26 ``flagged_no_pr``
   rows (15%) were that blind spot, each one a local branch sitting at the
   exact head of a real PR — one open, two merged, one closed.
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

import hashlib
import re
from dataclasses import dataclass
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

# ...and the same ':' separates a branch from a DIGEST OF its path when ONE
# branch is checked out in several worktrees at once (`git worktree add
# --force`). A bare branch identity can therefore never be mistaken for a
# discriminated one, and a discriminated one can never be mistaken for a
# detached key: that would need a branch literally named "@detached" sharing a
# path with a detached worktree, and a single path is either detached or on a
# branch, never both.
DUPLICATE_KEY_SEP = ":"

# ...and once more for an identity that cannot be emitted as itself at all. A
# worktree PATH may contain anything, and a BRANCH name may legally contain a
# bidi override or a zero-width space (MEASURED on git 2.43 — check-ref-format
# bans control characters and says nothing about the Cf category), so the
# natural identity is sometimes a value that cannot safely be a key OR be shown
# to a reader. It becomes an opaque digest rather than being thrown away.
OPAQUE_KEY_PREFIX = "@opaque:"


def _path_digest(path: str) -> str:
    """A worktree path rendered as something that is always safe to be a key.

    The path itself is NOT used, and the reason is a false suppression this
    very discriminator introduced before it was caught. A worktree path may
    legally contain a newline — this subsystem's own worktree parser was
    redesigned around exactly that — and an identity carrying a control or
    invisible character cannot be stored as a key and emitted verbatim to a
    model. Splicing a raw path into a branch-keyed identity therefore produced a
    value the classifier had to refuse, and a refused worktree landed in neither
    ``present`` nor ``held``, so ``apply_sweep`` resolved a live finding about
    uncommitted work.

    That refusal is GONE — ``worktree_identity`` now derives an opaque key for
    any unsafe identity rather than dropping it (see ``OPAQUE_KEY_PREFIX``), so
    this digest is no longer what stands between a duplicated worktree and a
    resolved row. It is kept because it is still the right key: stable, opaque
    and collision-resistant, so the duplicate case never DEPENDS on the safety
    net. Stated in the past tense on purpose — describing a quarantine that no
    longer exists is how the next reader trusts a guarantee nothing provides
    (cross-model review).

    The readable value is not lost — the finding row carries ``worktree_path``,
    and every surface renders THAT through ``neutralise``.

    The digest is FULL, never shortened. A key is the one thing that must not
    be truncated: two paths colliding on a prefix would silently transfer one
    worktree's acknowledgement to another worktree's work, which is the same
    rule ``dirty_state_key`` states for the same reason.
    """
    return hashlib.sha256(path.encode()).hexdigest()


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
# How far ahead of the clock a timestamp may legitimately sit. Not a guess at
# how wrong a clock can be — it is the skew between the clock that WROTE the
# value and the one reading it. Git commit dates come from this machine, mtimes
# come from this filesystem, and `mergedAt` comes from GitHub, so seconds of NTP
# and network drift are normal and minutes are not. Generous enough that a
# commit made moments ago is never mistaken for a corrupt one, tight enough that
# nothing hides behind it for long.
FUTURE_SKEW_TOLERANCE = timedelta(minutes=5)


def not_future(parsed: datetime | None, now: datetime) -> datetime | None:
    """*parsed*, or None when it sits implausibly far ahead of *now*.

    Git accepts a future commit date, a restored snapshot or a backwards clock
    step produces future mtimes, and a hand-repaired run record can carry
    anything. Every age gate in this subsystem asks "is this NEWER than the
    cutoff", so a future timestamp answers YES forever — and the two gates that
    ask it HOLD their subject, which means neither reported nor resolved, for as
    long as the wrong timestamp stands. A tip dated 2031 is not stranded work
    that resolves itself in five years; it is stranded work nobody is told about
    (Codex P2, PR #1794).

    Returning None rather than a clamped value is what keeps this from inventing
    a state: None is already what an UNPARSEABLE timestamp yields, and every
    caller already handles it — `classify_branches` judges such a branch "on its
    merits rather than excused", which FLAGS, and flagging is the direction a
    detector is allowed to be wrong in.
    """
    if parsed is None:
        return None
    return None if parsed - now > FUTURE_SKEW_TOLERANCE else parsed


PUSH_EXACT = "exact"  # local tip IS the remote tip: nothing is local-only
PUSH_BEHIND = "behind"  # remote has moved on; every local commit is pushed
PUSH_DIVERGED = "diverged"  # PROVEN local-only commits (non-merge, so real work)
PUSH_ABSENT = "absent"  # no remote branch of this name (never pushed, or deleted)
PUSH_UNKNOWN = "unknown"  # differs, but ancestry was unanswerable


@dataclass(frozen=True)
class PrIndex:
    """PR history addressed BOTH ways — by head-ref name and by head SHA.

    Two maps rather than one because they are different tiers of evidence and
    the module's whole design is that the stronger one must not sit behind the
    weaker. ``by_name`` is the indexing convenience (tier 5); ``by_head_sha``
    is SHA proof (tier 1), and a branch RENAMED after its PR merged matches
    only through it — the historical ``headRefName`` is gone, but
    ``headRefOid`` is immutable and still equals the local tip.

    ``for_branch`` is the only supported lookup, and that is deliberate. A
    caller reaching into ``by_name`` directly is the mechanism this class
    exists to retire: the SHA index would then be something every call site
    had to REMEMBER to consult, which is a convention, and a convention is
    what a reviewer finds one missing instance of at a time.
    """

    by_name: dict[str, list[dict]]
    by_head_sha: dict[str, list[dict]]

    def for_branch(self, branch: str | None, tip_sha: str | None = None) -> list[dict]:
        """Every PR row that could speak to this branch, name rows first.

        Name rows keep their listing order and lead, so a branch that was never
        renamed sees exactly the sequence it saw before the SHA index existed —
        the union can only ADD evidence, never reorder what was already there.

        Deduplicated by object identity rather than by ``number``: both maps are
        built in a single pass over one list and therefore hold the SAME dict
        objects, and every one of them is kept alive by the maps themselves for
        this index's whole lifetime, so ``id()`` cannot be recycled underneath
        us. ``number`` would have been the obvious key and is the wrong one — it
        is untrusted input and may be missing, in which case every unnumbered
        row would collapse onto a single ``None``.
        """
        rows = list(self.by_name.get(branch, [])) if branch else []
        if not tip_sha:
            return rows
        seen = {id(row) for row in rows}
        rows.extend(row for row in self.by_head_sha.get(tip_sha, []) if id(row) not in seen)
        return rows


def index_prs_by_head(prs: list[dict], *, owner: str | None = None) -> tuple[PrIndex, int]:
    """Index PR records by head-ref NAME and by head SHA. ``(index, ignored)``.

    Rows without a head-ref name are dropped FROM THE NAME MAP but kept in the
    SHA map — defence in depth rather than an observed case: the only
    production producer (``repo_pulse_gh.list_all_prs``) already drops every row
    lacking a non-empty string ``headRefName`` before the classifier sees it, so
    no live sweep can currently reach that branch.

    When *owner* is given, PRs whose head branch lives in a DIFFERENT account
    are excluded from the join entirely and counted: a contributor's fork branch
    named ``patch-1`` says nothing about a local ``patch-1``, and head-ref name
    reuse is already
    MEASURED at 35 of 1586 names here. Excluding them is cheap insurance —
    MEASURED 2026-09-06, 9 of 1665 PRs come from forks and none currently
    collides with a local branch name, so this closes a real hole at zero
    present cost.

    The fork filter governs BOTH maps, which is a choice worth stating because
    the SHA map could defensibly keep forks: a fork PR whose ``headRefOid`` IS
    our tip holds that exact commit, so it is genuine evidence rather than a
    name coincidence. It is excluded anyway because including it would open a
    new SUPPRESSION path on third-party data for a case nothing has yet
    observed, and the wrong direction here is the silent one. Revisit with a
    measurement, not with an argument.

    "Governs both maps" describes the filter's REACH, not its strength: the
    filter itself fails OPEN on an owner it cannot read. ``headRepositoryOwner``
    is absent for a PR whose fork has been DELETED, which
    ``repo_pulse_gh.list_all_prs`` records as ``None``, and a non-string owner
    is kept rather than excluded — uniformly in both maps, and uncounted.
    Pre-existing, unmeasured, and named here so the sentence above is not read
    as a guarantee it does not make.

    ``owner=None`` keeps every PR, for a caller that could not resolve the
    repository owner. That is the safe direction: an over-broad join can only
    SUPPRESS, and a suppression here is visible in the stage counts, whereas
    dropping every PR would flag the entire branch list at once.
    """
    by_name: dict[str, list[dict]] = {}
    by_head_sha: dict[str, list[dict]] = {}
    ignored = 0
    for pr in prs:
        head = pr.get("headRefName")
        oid = pr.get("headRefOid")
        named = isinstance(head, str) and bool(head)
        shaed = isinstance(oid, str) and bool(oid)
        if not (named or shaed):
            continue
        if owner is not None:
            pr_owner = pr.get("headRepositoryOwnerLogin")
            if isinstance(pr_owner, str) and pr_owner.lower() != owner.lower():
                ignored += 1
                continue
        if named:
            by_name.setdefault(head, []).append(pr)
        if shaed:
            by_head_sha.setdefault(oid, []).append(pr)
    return PrIndex(by_name=by_name, by_head_sha=by_head_sha), ignored


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
            #
            # An exact head SHA settles the same question DIRECTLY and outranks
            # push state, which is tier 3 evidence read off a ref of this NAME.
            # `headRefOid` is the commit GitHub holds as this PR's head, so an
            # exact match proves the tip is on the server whatever a same-named
            # remote ref does or does not say — and under a RENAME there is no
            # such ref to consult, which is exactly when this row arrived by SHA
            # rather than by name. Not gated on HOW the row was found: the
            # evidence is identical either way, and gating on provenance would
            # be the name-as-identity mistake one level up.
            if not (proven_pushed or (head and head == tip_sha)):
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
    # Positive containment is scanned for across EVERY row before any negative
    # verdict is chosen. Head-ref names are reused — MEASURED 35 of 1586 names
    # here, one carrying 7 PRs — so a branch can carry several unrelated closed
    # PRs, and the listing order is not evidence about anything. Returning on
    # the first disproven row let one stale PR override SHA proof sitting in
    # another, which files a stranded finding for work a closed PR provably did
    # contain (Codex P2, PR #1794). The first disproven row is still what gets
    # REPORTED; it just no longer gets to decide.
    unanswerable = False
    disproven: dict | None = None
    for pr in closed_rows:
        verdict = contains(pr.get("headRefOid"))
        if verdict is True:
            return "closed", {"pr": pr.get("number"), "proof": "head_oid_or_ancestor"}
        if verdict is False:
            if disproven is None:
                disproven = {
                    "pr": pr.get("number"),
                    "proof": "not_an_ancestor_of_the_closed_head",
                }
            continue
        unanswerable = True
    if disproven is not None:
        return "closed_local_only", disproven

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
        tip_date = not_future(_parse_iso(row.get("tip_date")), now)
        if tip_date is not None and tip_date > cutoff:
            # Work in flight right now is not stranded work. An UNDATED tip
            # (unparseable, or dated implausibly far AHEAD — git accepts a
            # future commit date, and this gate would then read "too new"
            # forever) is judged on its merits rather than excused.
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
            index.for_branch(branch, row.get("tip_sha")),
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

    The branch name when there is one — a worktree is USUALLY one-to-one with
    its branch, and the path can change while the work does not. A DETACHED
    worktree has no branch, so it keys on its path behind a prefix containing
    ':', which git forbids in a ref name, so the two spaces cannot collide.

    "Usually" is where this went wrong. ``git worktree add --force`` checks out
    a branch that is ALREADY checked out elsewhere, so two worktrees can carry
    the same branch and different uncommitted work — and they then collapse
    onto one ``(class, branch)`` key, where ``apply_sweep`` keeps the first
    sighting and the second can never be acknowledged or tracked separately
    (Codex P2, PR #1794). Such a worktree therefore takes a discriminator,
    behind the same ':' that makes the detached key collision-proof — a DIGEST
    of the path rather than the path, for the reason ``_path_digest`` states.

    The discriminator is CONDITIONAL, and that is load-bearing rather than
    tidy: the identity IS the ack key, so applying it unconditionally would
    change every existing worktree identity at once and silently expire every
    acknowledgement ever written. MEASURED 2026-09-12: 0 of 165 worktrees on
    this install share a branch, so the conditional form is a no-op here by
    construction and only starts acting when the ambiguity it answers actually
    exists.

    Being conditional has its own cost, which is smaller than the unconditional
    one but is not zero and is stated rather than left to be discovered: at the
    TRANSITION — a branch becoming duplicated, or stopping — the identity
    changes shape, so the old row is absent from ``present``, ``apply_sweep``
    resolves it and its acknowledgement goes with it. The work stays visible (a
    new row opens immediately), so the direction is safe, but that run publishes
    a ``resolved`` for a condition that did not end.

    ``branch_duplicated`` is stamped by ``list_worktrees`` over the FULL
    listing, not derived here from whatever subset a caller holds. That matters
    because the callers hold different subsets — the worker's HOLD path sees
    every registration including prunable ones, while ``classify_worktrees``
    sees only the ones it could read — and two populations would disagree about
    which branches are duplicated, producing a hold key that matches no finding.
    An identity computed one way in one place and another way in the other holds
    a key nothing matches, silently restoring the resolve-on-absence behaviour
    the hold exists to prevent.
    """
    branch = observation.get("branch")
    if not branch:
        natural = f"{DETACHED_KEY_PREFIX}{observation['path']}"
    elif observation.get("branch_duplicated"):
        natural = f"{branch}{DUPLICATE_KEY_SEP}{_path_digest(observation['path'])}"
    else:
        natural = branch
    if _safe_identity(natural):
        return natural
    # DERIVED, not refused. An identity that cannot round-trip safely used to be
    # QUARANTINED — counted, then dropped into neither `present` nor `held`, so
    # `apply_sweep` resolved it and the uncommitted work it named left the board
    # silently (Codex P2, PR #1794). Refusing to make a key is only correct when
    # the alternative is a key that lies; a digest is neither.
    #
    # Collision-resistant and stable, so an acknowledgement granted against it
    # holds, and prefixed with the same forbidden ':' that keeps every other
    # derived identity unrepresentable as a ref name. The human-readable form is
    # not lost: the finding carries `worktree_path`, which every surface renders
    # through `neutralise`.
    return f"{OPAQUE_KEY_PREFIX}{_path_digest(natural)}"


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
    opaque_identities = 0
    stages = dict.fromkeys(
        ("worktrees_total", "clean", "too_young", "flagged_dirty"), 0
    )
    stages["worktrees_total"] = len(observations)

    for obs in observations:
        identity = worktree_identity(obs)
        if identity.startswith(OPAQUE_KEY_PREFIX):
            # COUNTED, not dropped — and that is the correction. The identity is
            # the ACK KEY and round-trips verbatim through the MCP surface, so
            # it cannot be sanitised without merging two identities onto one
            # key; the previous resolution was to refuse it, which meant the
            # worktree entered neither `present` nor `held` and `apply_sweep`
            # RESOLVED it. `worktree_identity` now derives an opaque key
            # instead, so the row survives and this is a META count of how many
            # identities had to be made opaque. It is deliberately NOT one of
            # the terminal stages any more: the worktree still lands in exactly
            # one of clean/too_young/flagged_dirty, so those still sum to the
            # denominator. MEASURED 2026-09-06: 0 of 161 worktrees here.
            opaque_identities += 1
        entries = obs.get("entries") or []
        if not entries:
            stages["clean"] += 1
            continue
        # Same future-proofing as the branch gate: a restored snapshot or a
        # backwards clock step yields a future mtime, which would hold this
        # worktree out of the board for as long as the date stands.
        newest = not_future(obs.get("newest_mtime"), now)
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

    return {
        "findings": findings,
        "stages": stages,
        "held": held,
        # META, outside the terminal sum — like `ignored_forks` on the branch
        # side. Putting it in `stages` would break the invariant that the
        # terminal counts add up to `worktrees_total`.
        "opaque_identities": opaque_identities,
    }
