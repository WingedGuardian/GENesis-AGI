"""Late findings the round reflection owes, and the reflections that answer them.

``review_budget`` decides, per finding review, whether it was ``late`` (it
landed on a head after one of that head's descendants was already reviewed, so
it opened no round) and, per round, when it opened (``opened_at``). This module
reads those facts and never re-derives them: membership in ``rounds[].reviews``
does not mean "not late", and review marks carry no reviewer.

From ``review_reflection.REFLECTION_CUTOVER_ISO`` on, a finding outside the
open round is owed when it was late, or when it opened its round but arrived
after a fix to its head existed (owner ruling e70d4934). Each owed key is
credited only by a reflection judged under ITS OWN round's context: that
round's number, the classes of the reflections that are git ancestors of the
answering one, and evidence written inside that round's window. The open round
is never judged here; ``review_reflection.covered_keys`` keeps judging it as
before. With a cutover set, though, the whole owed list is unknown whenever the
late debt is, even if the open round's own debt is known.

``review_reflection`` imports this module only when a cutover is set, so with
the cutover None (as shipped) none of this runs.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import review_reflection as rr

Ancestors = Callable[[str], "frozenset[str] | None"]


@dataclass(frozen=True)
class RoundContext:
    """What a round owes, as of a time. ``round_number`` 0 is a late review
    with no earlier round: nothing more than the grammar is owed (owner,
    2026-10-12: a late-only answer owes no new audit or premise check)."""

    round_number: int
    round_started: str | None
    prior_heads: tuple[str, ...]


def _ancestry(budget: Mapping[str, Any]) -> tuple[set[str], Ancestors]:
    """The PR's commits and ``ancestors(sha)``: the strict ancestors inside the
    PR, or None when they are unknown (a commit outside the graph, or one whose
    parents were not read). Unknown ancestry never credits a key."""
    parents: dict[str, list[str] | None] = {}
    for row in budget.get("commits") or []:
        if isinstance(row, Mapping) and isinstance(row.get("sha"), str):
            given = row.get("parents")
            parents[row["sha"]] = list(given) if isinstance(given, list) else None
    memo: dict[str, frozenset[str] | None] = {}

    def ancestors(sha: str) -> frozenset[str] | None:
        if sha in memo:
            return memo[sha]
        if sha not in parents:
            return None
        seen: set[str] = set()
        stack = [sha]
        while stack:
            given = parents[stack.pop()]
            if given is None:
                memo[sha] = None
                return None
            for parent in given:
                # A parent outside the PR is the base: the walk stops there.
                if parent in parents and parent not in seen:
                    seen.add(parent)
                    stack.append(parent)
        memo[sha] = frozenset(seen)
        return memo[sha]

    return set(parents), ancestors


def _descends(ancestors: Ancestors, sha: str, base: str) -> bool:
    """Whether ``sha`` is ``base`` or a known descendant of it."""
    if sha == base:
        return True
    found = ancestors(sha)
    return found is not None and base in found


def _round_times(budget: Mapping[str, Any]) -> dict[str, datetime | None] | None:
    """Each round head's ``opened_at``, None where the producer could not tell.
    None overall for a budget that does not carry the field (an older
    ``review_budget``) or carries an unreadable one: never a guessed order."""
    times: dict[str, datetime | None] = {}
    for entry in budget.get("rounds") or []:
        if not isinstance(entry, Mapping) or "opened_at" not in entry:
            return None
        head = entry.get("head")
        if not isinstance(head, str):
            return None
        raw = entry["opened_at"]
        if raw is None:
            times[head] = None
            continue
        when = rr._parse_when(raw)
        if when is None:
            return None
        times[head] = when
    return times


def _opened_before(
    first: datetime | None, second: datetime | None, legacy_cut: datetime
) -> bool | None:
    """Whether a round opened at ``first`` opened strictly before one opened at
    ``second``; None when that cannot be told. Only a legacy round (one the old
    rule counted, from evidence before ``review_budget.ROUND_RULE_CUTOVER_ISO``)
    can have no time, so it opened before every round opened after that
    cutover, and is unordered against every other legacy round. Equal times are
    unordered too: a tie is never broken by head."""
    if first is None and second is None:
        return None
    if first is None:
        return True if second is not None and second >= legacy_cut else None
    if second is None:
        return False if first >= legacy_cut else None
    if first == second:
        return None
    return first < second


def round_context(budget: Mapping[str, Any], round_head: str, at: datetime) -> RoundContext | None:
    """Round ``round_head``'s context as of ``at``: its number (one plus the
    rounds that opened before it), its opening time, and the heads of those
    earlier rounds. None when it is not a round head, opened after ``at``, or
    its own position is undetermined (its ``opened_at`` unknown, or tied with
    or unordered against another round). Another round's unknown time costs
    nothing when its order against this one is still known."""
    import review_budget  # noqa: PLC0415

    times = _round_times(budget)
    legacy_cut = rr._parse_when(review_budget.ROUND_RULE_CUTOVER_ISO)
    if times is None or legacy_cut is None or round_head not in times:
        return None
    own = times[round_head]
    if own is None or own > at:
        return None
    prior = []
    for head, when in times.items():
        if head == round_head:
            continue
        before = _opened_before(when, own, legacy_cut)
        if before is None:
            return None
        if before:
            prior.append(head)
    return RoundContext(len(prior) + 1, own.isoformat(), tuple(sorted(prior)))


def _contexts(
    budget: Mapping[str, Any], item: Mapping[str, Any], at: datetime, ancestors: Ancestors
) -> list[RoundContext] | None:
    """Every context a late key must be answered under. Its own round when it
    has one. With none (``round_head`` null): round 0 when no earlier round is
    its ancestor, else every newest candidate round (the two sides of a merge),
    all of which must pass. None when its round cannot be placed."""
    if item.get("round_head"):
        context = round_context(budget, str(item["round_head"]), at)
        return [context] if context else None
    found = ancestors(str(item.get("head")))
    times = _round_times(budget)
    if found is None or times is None:
        return None
    older = {head for head in times if head in found}
    if not older:
        return [RoundContext(0, item["at"].isoformat(), ())]
    if any(ancestors(head) is None for head in older):
        return None
    newest = [
        head
        for head in older
        if not any(head in (ancestors(other) or ()) for other in older - {head})
    ]
    contexts = [round_context(budget, head, at) for head in sorted(newest)]
    if any(context is None for context in contexts):
        return None
    return [context for context in contexts if context is not None]


def late_findings(
    budget: Mapping[str, Any], cutover: str | None, fix_time: Any = None
) -> list[dict[str, Any]] | None:
    """The finding keys owed besides the open round, one entry per key (the
    finding plus ``key`` and ``at``), from reviews submitted at or after
    ``cutover``. ``[]`` when ``cutover`` is None.

    A finding on a head outside the commit graph (a force-push dropped it,
    owner ruling a3a676b6) is dropped FIRST, before its time or keys are read.
    A late review (``late`` True) is always owed. One that opened its round
    was the open round's to answer while it was open; it is owed here only
    when it arrived after a fix to its head (``fix_time(head)``, None when
    nothing has been fixed since). A key the open round owes is never counted
    twice.

    None (unknown) for a budget that is not ``ok``, an unreadable cutover, an
    in-graph finding from the cutover on with an unreadable time, a key that
    could not be derived on a finding that is owed, a ``late`` that is not a
    bool (an older budget), a fix time that is needed with no ``fix_time``, or
    an owed key whose round cannot be placed. ``late_state`` is deliberately not read: it also covers
    findings off the graph or before the cutover, which owe nothing here."""
    if cutover is None:
        return []
    start = rr._parse_when(cutover)
    if start is None or budget.get("status") != "ok":
        return None
    own = set(budget.get("open_keys") or []) if budget.get("round_state") == "open" else set()
    in_graph, ancestors = _ancestry(budget)
    found: dict[str, dict[str, Any]] = {}
    for item in budget.get("findings") or []:
        if not isinstance(item, Mapping):
            return None
        head = item.get("head")
        if not isinstance(head, str) or head not in in_graph:
            continue
        when = rr._parse_when(item.get("submitted_at"))
        if when is None:
            return None
        if when < start:
            continue
        late = item.get("late")
        if not isinstance(late, bool):
            return None
        # Whether the finding is owed at all comes first: a finding that owes
        # nothing never makes owed unknown, even with a key that could not be
        # derived.
        if not late:
            if fix_time is None:
                return None
            fixed = fix_time(head)
            if fixed is None or when <= fixed:
                continue
        keys = item.get("keys")
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            return None
        for key in keys:
            if key not in own and key not in found:
                found[key] = {**item, "key": key, "at": when}
    # Every owed key must have a round to be answered under. One that cannot
    # be placed makes owed unknown, never a key no reflection could ever clear.
    latest = datetime.max.replace(tzinfo=UTC)
    if any(_contexts(budget, item, latest, ancestors) is None for item in found.values()):
        return None
    return list(found.values())


def late_owed(
    budget: Mapping[str, Any],
    covered_late: Iterable[str],
    *,
    cutover: str | None,
    fix_time: Any = None,
) -> tuple[list[str] | None, list[str] | None]:
    """``(late_keys, owed)``: every late key from ``cutover`` on, and those not
    in ``covered_late``. ``(None, None)`` when unknown, never a known empty
    next to an unknown."""
    late = late_findings(budget, cutover, fix_time)
    if late is None:
        return None, None
    keys = [item["key"] for item in late]
    done = set(covered_late)
    return keys, [key for key in keys if key not in done]


def _fix_times(cwd: str, base: str, head: str) -> list[datetime]:
    """The author time of every fix on the LINE from ``base`` to ``head``
    (``--ancestry-path``: descendants of ``base`` that ``head`` reaches, so a
    side branch's own commits drop out and the merge that brought it in
    stays): each commit that is a merge or changes the tree (an empty
    reflection commit is no fix). The owe rule (earliest fix to the current
    head) and the credit rule (latest fix to the reflection's head) read the
    same line, so a key owed is answerable until the next fix lands. A fix
    committed while a key is owed leaves it owed for good: the commit gate
    that sets the cutover must stop that commit. The AUTHOR time, the earlier
    of the two: amending a fix rewrites only its committer time (owner ruling
    2026-10-13). Raises ``Refused`` when git cannot say."""
    code, out, _ = rr._run(
        ["git", "-C", cwd, "log", "--ancestry-path", "--format=%H %T %P|%at", f"{base}..{head}"]
    )
    if code != 0:
        raise rr.Refused(f"git log {base[:12]}..{head[:12]} failed")
    times = []
    for row in out.split("\n"):
        if not row.strip():
            continue
        names, _, stamp = row.partition("|")
        _, tree, *parents = names.split()
        if (
            len(parents) == 1
            and tree == rr._git(cwd, "rev-parse", f"{parents[0]}^{{tree}}").strip()
        ):
            continue
        if not stamp.strip().isdigit():
            raise rr.Refused(f"a fix after {base[:12]} has no readable author time")
        times.append(datetime.fromtimestamp(int(stamp), UTC))
    return times


def fix_time_reader(cwd: str, current: str) -> Callable[[str], datetime | None]:
    """``fix_time`` for ``late_findings``: the earliest fix after a head on the
    way to ``current``, read from local git once per head."""
    memo: dict[str, datetime | None] = {}

    def fix_time(head: str) -> datetime | None:
        if head not in memo:
            times = _fix_times(cwd, head, current)
            memo[head] = min(times) if times else None
        return memo[head]

    return fix_time


def _is_ancestor(cwd: str, sha: str, of: str) -> bool:
    """Whether commit ``sha`` is an ancestor of ``of`` (git's exit 0 or 1).
    Any other exit refuses: an unreadable history is unknown, never "no"."""
    code, _, _ = rr._run(["git", "-C", cwd, "merge-base", "--is-ancestor", sha, of])
    if code in (0, 1):
        return code == 0
    raise rr.Refused(f"git merge-base --is-ancestor {sha[:12]} {of[:12]} failed")


def _prior_labels(
    cwd: str,
    log: Sequence[tuple[str, str, str, datetime | None]],
    prior_heads: Sequence[str],
    sha: str,
) -> list[str]:
    """The earlier rounds' classes as reflection ``sha`` saw them: only
    reflections that are strict git ancestors of it count, never one that
    reached the history later through a merge. Ancestry is probed only for
    rows naming one of ``prior_heads``."""
    wanted = set(prior_heads)
    rows = [
        row
        for row in log
        if row[0] != sha and rr.parse(row[2]).head in wanted and _is_ancestor(cwd, row[0], sha)
    ]
    return rr._class_labels(rows, prior_heads)


def covered_late_keys(
    cwd: str,
    budget: Mapping[str, Any],
    late: Sequence[Mapping[str, Any]],
    *,
    gate_lane: bool,
    acceptance: Sequence[str] | None,
    deadline: float | None = None,
) -> set[str]:
    """Late keys answered by committed reflections. A reflection R naming head
    N, committed at T, credits late key k (reviewed on head H at time S) only
    when ALL hold:

    - R is an empty commit made before any fix to N (``before_any_fix``);
    - N is the current head or in its history, and N is H or descends from it
      (from the budget's commit graph; unknown ancestry never credits);
    - S is no later than T: an answer cannot predate its finding;
    - when N is not H, S is after the LATEST fix on the way from H to N (author
      time). A review that arrived before a fix was owed before it, and a
      reflection made after that fix is a rationalisation, not a reflection;
    - ``check`` passes R under k's own round's context (every candidate round
      when k's round is ambiguous): that round's number, the classes of the
      reflections that are git ancestors of R and name an earlier round, the
      PR's acceptance points, and evidence written from that round's start to
      T. Round 0 (a late review with no earlier round) owes the grammar only.

    Returns only keys from ``late``; the open round's coverage is never mixed
    in. ``deadline`` is a ``time.monotonic()`` bound; past it the check
    refuses. Raises ``Refused`` when git cannot be read: unknown, never
    nothing owed."""
    current = budget.get("current_head")
    in_graph, ancestors = _ancestry(budget)
    log = rr._log_reflections(cwd)
    labels: dict[tuple[tuple[str, ...], str], list[str]] = {}
    checked: dict[tuple[str, RoundContext], bool] = {}
    fix_times: dict[tuple[str, str], datetime | None] = {}
    covered: set[str] = set()
    for sha, kind, body, made_at in log:
        if deadline is not None and time.monotonic() > deadline:
            raise rr.Refused("the late-findings check ran out of time")
        parsed = rr.parse(body)
        head = parsed.head
        if kind != "empty" or made_at is None or not head:
            continue
        if not (head == current or _descends(ancestors, str(current), head)):
            continue
        if not rr.before_any_fix(cwd, head, sha):
            continue
        for item in late:
            key = item["key"]
            if key in covered or key not in parsed.keys:
                continue
            reviewed = str(item.get("head"))
            if reviewed not in in_graph or not _descends(ancestors, head, reviewed):
                continue
            if item["at"] > made_at + rr._COMMIT_CLOCK_SLACK:
                continue
            if head != reviewed:
                if (reviewed, head) not in fix_times:
                    found = _fix_times(cwd, reviewed, head)
                    fix_times[(reviewed, head)] = max(found) if found else None
                fixed = fix_times[(reviewed, head)]
                if fixed is not None and item["at"] <= fixed:
                    continue
            contexts = _contexts(budget, item, made_at, ancestors)
            if not contexts:
                continue
            passed = True
            for context in contexts:
                if (sha, context) not in checked and context.round_number == 0:
                    checked[(sha, context)] = parsed.ok and not parsed.escalate
                if (sha, context) not in checked:
                    if (context.prior_heads, sha) not in labels:
                        labels[(context.prior_heads, sha)] = _prior_labels(
                            cwd, log, context.prior_heads, sha
                        )
                    checked[(sha, context)] = rr.check(
                        body,
                        head=head,
                        round_number=context.round_number,
                        gate_lane=gate_lane,
                        previous_classes=labels[(context.prior_heads, sha)],
                        acceptance=acceptance,
                        round_started=context.round_started,
                        made_at=made_at,
                        cwd=cwd,
                    ).ok
                passed = passed and checked[(sha, context)]
            if passed:
                covered.add(key)
    return covered


def late_status(
    cwd: str,
    budget: Mapping[str, Any],
    repo: str,
    number: int,
    *,
    now: datetime,
    cutover: str,
    deadline: float | None = None,
) -> dict[str, Any]:
    """``review_reflection.status`` with a cutover: the open round judged as
    without one, plus the late keys from ``cutover`` on, each credited only
    under its own round; owed is unknown whenever the late debt is.
    ``deadline`` (``time.monotonic()``) bounds the late-credit walk. Raises
    ``Refused`` when git cannot be read."""
    ok = budget.get("status") == "ok"
    fix_time = fix_time_reader(cwd, str(budget.get("current_head"))) if ok else None
    state = rr.owed_state(budget, (), now=now, cutover=cutover, fix_time=fix_time)
    is_open = ok and state["round_state"] == "open"
    late = late_findings(budget, cutover, fix_time) if ok else []
    if not (is_open or late):
        return state
    acceptance = rr._acceptance(budget, repo, number)
    covered = rr._open_covered(cwd, budget, state, acceptance) if is_open else set()
    late_covered = (
        covered_late_keys(
            cwd,
            budget,
            late,
            gate_lane=state["gate_lane"],
            acceptance=acceptance,
            deadline=deadline,
        )
        if late
        else set()
    )
    return rr.owed_state(
        budget,
        covered,
        now=now,
        cutover=cutover,
        fix_time=fix_time,
        late_covered=late_covered,
    )
