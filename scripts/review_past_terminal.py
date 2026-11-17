#!/usr/bin/env python3
"""The past-round-4 rules for a round reflection (#4133).

The ordinary lane's terminal round is 4. Past it, a reflection that keeps
fixing is held to four rules, each read from the review budget and the one
reflection being judged, never from another reflection:

- **P** (a PR past round 3): a fix-now owes ``Prediction: merges at round <the
  next round> because <why>`` and ``Confidence: <95-100>% falsified-by <what>``.
- **H** (a round from 5 on): ``Prediction-held: yes|no <why>``; ``yes`` is
  refused when the round drew a floor finding, a level is unreadable, or the
  round has not settled.
- **F**: once two rounds from 5 on drew a floor finding, the only admissible
  decision is ``send-back``.
- **E** (a round from 5 on that drew no floor finding): no fix-now for that
  round's keys; file them or send the PR back.

Past round 3 a send-back fixes nothing now. A send-back that files every
finding and writes ``Prediction-held: no`` passes every rule in every state.

Which rounds count. A round counts for the rules only when its head is still in
the PR's commit graph (``live``) and a fix produced it (``advanced_by_fix``):
some commit between the previous live round head and this head changes its
tree and is not a merge with a parent outside the PR (a merge of main). A round
a merge or an empty commit produced, or one on a head a force-push dropped,
never ends fixing and never counts as a failed prediction; its numbering still
counts every round. When the budget cannot tell whether a fix produced the
newest such round, the rules are off for the PR (they fail toward main's
behaviour); the newest round is never taken from an older one instead.

"Floor" is the level the reviewer wrote (``review_budget`` ``findings[].
blocking``), for every rule (owner rulings 2922a1fd, 2fd36db6). It does not
read ``off_diff``.

The rules bind only rounds opened at or after ``PAST_TERMINAL_CUTOVER_ISO``.
While it is None nothing binds: it is set once the review loop's commit-side end
has deployed, so the reflection rules never run ahead of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import review_late
import review_reflection as rr

#: The rules bind rounds opened from here on; None binds nothing. A round
#: opened earlier keeps the rules it was answered under, so reflections already
#: committed stay valid. It must lie after the deploy of the code that reads it.
PAST_TERMINAL_CUTOVER_ISO: str | None = None
#: The ordinary lane's terminal round is 4: the Prediction is owed from round 4
#: on, the held judgement and the end of fixing from round 5.
PREDICT_FROM = 4
JUDGE_FROM = 5
MIN_CONFIDENCE = 95


@dataclass(frozen=True)
class RoundRecord:
    """One review round as the rules read it, from the budget: its number and
    opening time, its findings ``(submitted_at, level, key)`` (late joiners
    included; an unreadable time or level is None), whether its head is still
    in the PR (``live``) and whether a fix produced it (``advanced_by_fix``,
    None when the budget cannot tell)."""

    head: str
    number: int
    opened_at: datetime | None
    findings: tuple[tuple[datetime | None, bool | None, str | None], ...]
    live: bool = True
    advanced_by_fix: bool | None = True

    @property
    def keys(self) -> set[str]:
        return {str(k) for _, _, k in self.findings if k is not None}

    @property
    def counts(self) -> bool:
        """Whether the rules read this round: live and produced by a fix."""
        return self.live and self.advanced_by_fix is True

    @property
    def settle_at(self) -> datetime | None:
        """30 minutes after its first finding review; None with no timed one,
        or when any finding's time is unreadable."""
        times = [when for when, _, _ in self.findings]
        if not times or any(when is None for when in times):
            return None
        return min(t for t in times if t is not None) + rr.SETTLE

    def floor(self, at: datetime) -> tuple[bool | None, list[str]]:
        """Whether a finding submitted at or before ``at`` is at the floor:
        True (with its keys), None when none is and one cannot be read, else
        False. A finding after ``at`` never changes an answer given at ``at``;
        an untimed one counts at every time. The one floor reading every rule
        shares."""
        seen = [(b, k) for when, b, k in self.findings if when is None or when <= at]
        hit = sorted(str(k) for b, k in seen if b is True)
        if hit:
            return True, hit
        return (None if any(b is None for b, _ in seen) else False), []


Records = Mapping[str, RoundRecord]


def _advanced(
    budget: Mapping[str, Any],
    ordered: list[tuple[str, int]],
    graph: set[str],
    ancestors: review_late.Ancestors,
) -> dict[str, bool | None]:
    """``advanced_by_fix`` per live round head (see the module docstring),
    from the budget's commit rows. True as soon as one commit in the range
    proves a fix; else None when anything in the range is unknown; else False.
    The first live round counts as produced by a fix."""
    trees: dict[str, str | None] = {}
    parents: dict[str, list[str] | None] = {}
    for row in budget.get("commits") or []:
        if isinstance(row, Mapping) and isinstance(row.get("sha"), str):
            tree = row.get("tree")
            trees[row["sha"]] = tree if isinstance(tree, str) and tree else None
            given = row.get("parents")
            parents[row["sha"]] = list(given) if isinstance(given, list) else None
    out: dict[str, bool | None] = {}
    previous: str | None = None
    for head, _ in ordered:
        if head not in graph:
            continue
        if previous is None:
            out[head] = True
            previous = head
            continue
        mine, theirs = ancestors(head), ancestors(previous)
        if mine is None or theirs is None:
            out[head] = None
            previous = head
            continue
        unknown = False
        proved = False
        for sha in ({head} | mine) - ({previous} | theirs):
            given = parents.get(sha)
            if given is None:
                unknown = True
                continue
            if len(given) > 1 and any(p not in graph for p in given):
                continue  # a merge of main (or another outside branch): no fix
            if not given or given[0] not in graph:
                unknown = True  # its first parent's tree is not in the budget
                continue
            mine_tree, base_tree = trees.get(sha), trees.get(given[0])
            if mine_tree is None or base_tree is None:
                unknown = True
                continue
            if mine_tree != base_tree:
                proved = True
                break
        out[head] = True if proved else (None if unknown else False)
        previous = head
    return out


def round_records(budget: Mapping[str, Any]) -> dict[str, RoundRecord] | None:
    """Every round, keyed by head: its number and opening time
    (``review_late.round_context``), its findings with their levels, and
    ``live`` and ``advanced_by_fix``. None when any round's place is
    undetermined (an untimed legacy round, a tie): the rules are then off for
    the PR (fails open)."""
    times = review_late._round_times(budget)
    if times is None:
        return None
    far = datetime.max.replace(tzinfo=UTC)
    numbered: list[tuple[str, int]] = []
    for head in times:
        context = review_late.round_context(budget, head, far)
        if context is None:
            return None
        numbered.append((head, context.round_number))
    numbered.sort(key=lambda pair: pair[1])
    graph, ancestors = review_late._ancestry(budget)
    advanced = _advanced(budget, numbered, graph, ancestors)
    out: dict[str, RoundRecord] = {}
    for head, number in numbered:
        found: list[tuple[datetime | None, bool | None, str | None]] = []
        for item in budget.get("findings") or []:
            if not isinstance(item, Mapping) or item.get("round_head") != head:
                continue
            when = rr._parse_when(item.get("submitted_at"))
            keys = item.get("keys") if isinstance(item.get("keys"), list) else []
            levels = item.get("blocking")
            if not isinstance(levels, list) or len(levels) != len(keys):
                levels = [None] * len(keys)
            for key, level in zip(keys, levels, strict=True):
                found.append((when, level if isinstance(level, bool) else None, key))
        live = head in graph
        out[head] = RoundRecord(
            head,
            number,
            times[head],
            tuple(found),
            live=live,
            advanced_by_fix=advanced.get(head) if live else False,
        )
    return out


def _bound(record: RoundRecord, cutover: str | None) -> bool:
    start = rr._parse_when(cutover)
    return start is not None and record.opened_at is not None and record.opened_at >= start


def next_round(records: Records, at: datetime) -> int:
    """The round the PR is in after ``at``: one more than the rounds opened by
    then, every round counted (a round with no opening time opened before)."""
    return 1 + sum(1 for r in records.values() if r.opened_at is None or r.opened_at <= at)


#: ``newest_under_rules`` when the newest round's fix cannot be told.
UNKNOWN = object()


def newest_under_rules(records: Records, at: datetime) -> RoundRecord | object | None:
    """The newest round opened by ``at`` that the rules read (live and
    produced by a fix). Rounds a fix did not produce are skipped; a round
    whose fix is unknown stops the search and answers ``UNKNOWN``, so an older
    round never stands in for it. None when no round qualifies."""
    opened = [r for r in records.values() if r.opened_at is None or r.opened_at <= at]
    for record in sorted(opened, key=lambda r: r.number, reverse=True):
        if not record.live or record.advanced_by_fix is False:
            continue
        if record.advanced_by_fix is None:
            return UNKNOWN
        return record
    return None


def _past_terminal_pr(
    records: Records, at: datetime, gate_lane: bool, cutover: str | None
) -> RoundRecord | None:
    """The newest round when the PR is past round 3 at ``at`` under the rules:
    ordinary lane, the newest round the rules read is round 4 or later and was
    opened at or after the cutover. Else None, so nothing binds."""
    if gate_lane:
        return None
    newest = newest_under_rules(records, at)
    if not isinstance(newest, RoundRecord):
        return None
    if newest.number < PREDICT_FROM or not _bound(newest, cutover):
        return None
    return newest


def prediction_failures(records: Records, at: datetime, cutover: str | None) -> int:
    """Rounds from round 5 on that the rules read, bound and opened by ``at``,
    that drew a floor finding by ``at``: each is a prediction that did not
    hold. From the budget alone, so no reflection can reset it; an unreadable
    level is not counted."""
    count = 0
    for record in records.values():
        if record.number < JUDGE_FROM or not record.counts or not _bound(record, cutover):
            continue
        if record.opened_at is None or record.opened_at > at:
            continue
        if record.floor(at)[0] is True:
            count += 1
    return count


#: ``problems``' default cutover: the module constant at call time, so a test
#: can pass ``None`` to mean "no cutover" whatever the constant says.
_MODULE_CUTOVER: Any = object()


def problems(
    parsed: rr.Reflection,
    record: RoundRecord | None,
    records: Records | None,
    *,
    gate_lane: bool,
    made_at: datetime | None,
    cutover: str | None | object = _MODULE_CUTOVER,
) -> list[str]:
    """The past-round-4 rules for a reflection answering ``record``'s keys.

    Nothing when the rules cannot be placed (``records`` None, an unreadable
    commit time), on the gate lane, before the cutover, or while the PR is not
    yet past round 3. The PR-wide phase reads the PR's newest round: a
    send-back fixes nothing now, the two-failure rule, and the Prediction and
    Confidence a fix-now owes. The per-round phase reads the key's own round
    (``record``, None for a late key with no round): Prediction-held and the
    end of fixing."""
    if cutover is _MODULE_CUTOVER:
        cutover = PAST_TERMINAL_CUTOVER_ISO
    if records is None or made_at is None:
        return []
    newest = _past_terminal_pr(records, made_at, gate_lane, cutover)
    if newest is None:
        return []
    found: list[str] = []
    sent_back = parsed.decision == "send-back"
    fix_now = {k for k, d in parsed.dispositions.items() if d.kind == "fix-now"}
    if sent_back and fix_now:
        found.append(
            f"past round {PREDICT_FROM - 1} a send-back fixes nothing now: file each finding "
            "('Disposition: <key> file issue=#<n>') instead of fix-now"
        )
    if fix_now and not sent_back:
        want = next_round(records, made_at)
        if parsed.prediction_round != want:
            found.append(
                f"past round {PREDICT_FROM - 1}, fixing again needs "
                f"'Prediction: merges at round {want} because <why>'"
            )
        if parsed.confidence is None or parsed.confidence < MIN_CONFIDENCE:
            found.append(
                f"fixing again past round {PREDICT_FROM - 1} needs "
                f"'Confidence: <{MIN_CONFIDENCE}-100>% falsified-by <what would show it wrong>'"
            )
    if not sent_back and prediction_failures(records, made_at, cutover) >= 2:
        found.append("two predictions past round 4 failed: the only decision left is 'send-back'")
    if (
        record is None
        or record.number < JUDGE_FROM
        or not record.counts
        or not _bound(record, cutover)
    ):
        return found
    level, keys = record.floor(made_at)
    if parsed.held is None:
        found.append(
            "round 5 on carries 'Prediction-held: yes|no <why>': did the last prediction hold?"
        )
    elif parsed.held == "yes":
        settle = record.settle_at
        if level is True:
            found.append(
                f"'Prediction-held: yes' is wrong: round {record.number} drew floor findings "
                f"({', '.join(keys)})"
            )
        elif level is None:
            found.append(
                "'Prediction-held: yes' cannot be verified (a finding's level is unreadable); "
                "write 'no'"
            )
        elif settle is None or made_at < settle:
            when = settle.isoformat()[:16] if settle else "?"
            found.append(
                f"'Prediction-held: yes' needs the round settled: wait until {when} UTC, "
                "or write 'no'"
            )
    if level is False and fix_now & record.keys and not sent_back:
        found.append(
            f"round {record.number} drew no floor finding, so fixing ends here: file each "
            "finding as an issue (merge-and-file) or send the PR back; no fix-now"
        )
    return found
