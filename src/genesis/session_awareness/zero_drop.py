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
name-free git signals were tried and all four failed; what works is a join on
PR HISTORY by head-ref name, with a time guard:

- an OPEN PR covers the branch (the work is in review, not stranded);
- a MERGED PR covers it ONLY if the merge POSTDATES the local tip — head-ref
  names get reused (MEASURED: 35 of 1586 names, one carrying 7 PRs), and
  commits land on a branch after its PR merges. That guard cost 1 of 115
  suppressions and the one it kept was a TRUE positive;
- a CLOSED-unmerged PR is a deliberate abandonment: suppressed, but COUNTED,
  so the suppression stays arithmetic rather than invisible.

Everything the run suppresses is reported as a stage count with its
denominator. There are no prefix denylists by owner decision: a backup or
scratch branch that flags is acknowledged with a reason (and because such a
branch never moves, the SHA-keyed ack never expires), which leaves a record of
the judgement instead of a rule nobody can see.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


def index_prs_by_head(prs: list[dict]) -> dict[str, list[dict]]:
    """Group PR records by ``headRefName``. Rows without one are dropped."""
    index: dict[str, list[dict]] = {}
    for pr in prs:
        head = pr.get("headRefName")
        if isinstance(head, str) and head:
            index.setdefault(head, []).append(pr)
    return index


def pr_coverage(prs_for_branch: list[dict], *, tip_date: datetime | None) -> tuple[str, dict]:
    """Does any PR on this head ref COVER the local tip?

    Returns ``(verdict, evidence)`` where verdict is one of ``open``,
    ``merged``, ``merged_predates_tip``, ``closed``, ``none``. The first three
    are ordered by strength: an open PR settles it, then a covering merge.
    ``merged_predates_tip`` means a PR for this name merged, but the local
    branch has moved SINCE — the time guard's true-positive shape, and the
    reason a merged PR is not on its own proof the work landed.
    ``merged_tip_undated`` is the same shape with the tip date missing, which
    is flagged rather than suppressed: see the comment at that branch.
    """
    merged_predates = False
    saw_closed = False
    saw_merged_undatable_tip = False
    for pr in prs_for_branch:
        state = (pr.get("state") or "").upper()
        if state == "OPEN":
            return "open", {"pr": pr.get("number"), "url": pr.get("url")}
        if state == "MERGED":
            merged_at = _parse_iso(pr.get("mergedAt"))
            if merged_at is None:
                continue  # a MERGED row with no mergedAt proves nothing
            if tip_date is None:
                # We KNOW the branch is ahead; we just could not date its tip,
                # so we cannot tell whether commits landed after the merge.
                # Suppressing here would clear a finding on evidence we failed
                # to collect — the wrong direction for a detector whose worst
                # outcome is a false clean board. Flag it, in its own bucket,
                # so the ambiguity is visible rather than folded into a verdict.
                saw_merged_undatable_tip = True
                continue
            if merged_at >= tip_date:
                return "merged", {"pr": pr.get("number"), "merged_at": pr.get("mergedAt")}
            merged_predates = True
        elif state == "CLOSED":
            saw_closed = True
    if merged_predates:
        return "merged_predates_tip", {}
    if saw_merged_undatable_tip:
        return "merged_tip_undated", {}
    if saw_closed:
        return "closed", {}
    return "none", {}


def classify_branches(
    branches: list[dict],
    *,
    remote_names: set[str],
    prs: list[dict],
    now: datetime,
    min_age_hours: int = 12,
) -> dict:
    """Split every local branch into findings + a full stage accounting.

    Returns ``{"findings": {class: [finding, ...]}, "stages": {...}}``. Every
    branch is counted in exactly one terminal stage, so the stage counts sum
    to the ref total — suppression you cannot add up is suppression you cannot
    audit.

    A branch whose ahead-count is UNKNOWN (an old git, a broken base ref) is
    counted as ``ahead_unknown``, is NOT a finding, and IS held: reporting
    stranded work on evidence we failed to collect would teach everyone to
    ignore the board, and RESOLVING an existing finding on that same missing
    evidence would quietly clear it. Both directions are wrong, and only the
    first is obvious.
    """
    index = index_prs_by_head(prs)
    cutoff = now - timedelta(hours=min_age_hours)
    findings: dict[str, list[dict]] = {CLASS_UNPUSHED: [], CLASS_PUSHED_NO_PR: []}
    held: set[str] = set()
    stages = dict.fromkeys(
        (
            "refs_total",
            "ahead_unknown",
            "not_ahead",
            "too_young",
            "covered_open_pr",
            "covered_merged_pr",
            "suppressed_closed_pr",
            "flagged_merge_predates_tip",
            "flagged_merged_tip_undated",
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

        verdict, evidence = pr_coverage(index.get(branch, []), tip_date=tip_date)
        if verdict == "open":
            stages["covered_open_pr"] += 1
            continue
        if verdict == "merged":
            stages["covered_merged_pr"] += 1
            continue
        if verdict == "closed":
            stages["suppressed_closed_pr"] += 1
            continue

        if verdict == "merged_predates_tip":
            stages["flagged_merge_predates_tip"] += 1
        elif verdict == "merged_tip_undated":
            stages["flagged_merged_tip_undated"] += 1
        else:
            stages["flagged_no_pr"] += 1

        pushed = branch in remote_names
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
                    **({"evidence": evidence} if evidence else {}),
                },
            }
        )

    return {"findings": findings, "stages": stages, "held": held}


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
    stages = dict.fromkeys(("worktrees_total", "clean", "too_young", "flagged_dirty"), 0)
    stages["worktrees_total"] = len(observations)

    for obs in observations:
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
