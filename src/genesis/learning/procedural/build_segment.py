"""The part of a session the lesson builder has not judged yet (#4807).

The whole-session builder re-judges every session's whole transcript on each
extraction cycle with new messages. ``cc_sessions.lesson_built_through`` records
the ``tool_use_id`` of the last tool entry already judged; :func:`select_part`
cuts the spine at that entry. Position, not time: tool-result timestamps are not
monotonic in a transcript, so a time cut could skip or repeat an entry.

Pure: no I/O, no clock. The extraction job decides what to do with the part.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from genesis.learning.procedural.struggle_detector import judge_line


@dataclass(frozen=True)
class Part:
    """What to judge next.

    ``new``: the entries after the marker that fit the budget, oldest first,
    tool and user entries alike (user corrections feed the struggle score).
    ``context``: up to ``context_n`` tool entries just before ``new``, already
    judged, shown so a struggle spanning two cycles reads whole.
    ``through_id``: the marker to store once ``new`` is judged: the last tool
    entry with an id in ``new``, or the old marker when there is none.
    ``reset``: the stored marker is not in the spine (the file was replaced),
    so ``new`` starts from the beginning.
    ``pending_tail``: entries from the first call with no result after the last
    user turn to the end, left for a later cycle because that call may still be
    running.
    ``held_back``: new entries past the budget, left for a later cycle.
    """

    new: list[dict] = field(default_factory=list)
    context: list[dict] = field(default_factory=list)
    through_id: str | None = None
    reset: bool = False
    pending_tail: int = 0
    held_back: int = 0


def _tool_id(entry: dict) -> str | None:
    tid = entry.get("tool_use_id") if entry.get("type") == "tool" else None
    return tid if isinstance(tid, str) and tid else None


def _cost(entry: dict) -> int:
    """Characters ``entry`` adds to the judge's view (user entries add none)."""
    return len(judge_line(entry)) + 1 if entry.get("type") == "tool" else 0


def select_part(
    spine: list[dict],
    marker: str | None,
    *,
    budget_chars: int,
    context_n: int = 20,
) -> Part:
    """Cut ``spine`` after ``marker`` and fit the rest to ``budget_chars``.

    The budget covers the rendered context and the new entries together, so the
    context is never cut to make room. Entries are taken up to and including the
    first tool entry with an id, even when that exceeds the budget, or one
    oversized call would hold the marker in place forever.
    """
    start, reset = 0, False
    if marker is not None:
        # The FIRST copy of a repeated id (a transcript can replay earlier calls):
        # cutting there can only re-judge, cutting at a later copy could skip the
        # entries between the copies unjudged.
        hit = next((i for i, e in enumerate(spine) if _tool_id(e) == marker), None)
        if hit is not None:
            start = hit + 1
        else:
            reset = True

    # A call without a result may still be running if no user turn follows it:
    # parallel calls' results can arrive out of order, so one answered call after
    # it does not prove it finished. Everything from the first such call waits.
    # A call followed by a user turn was cut short (a crash); the parser has
    # marked it, and it is judged.
    last_user = max(
        (i for i in range(start, len(spine)) if spine[i].get("type") == "user"),
        default=start - 1,
    )
    end = next(
        (
            i
            for i in range(last_user + 1, len(spine))
            if spine[i].get("type") == "tool" and not spine[i].get("has_result", True)
        ),
        len(spine),
    )
    pending_tail = len(spine) - end

    context = (
        [e for e in spine[:start] if e.get("type") == "tool"][-context_n:]
        if start and context_n > 0
        else []
    )
    left = budget_chars - (sum(_cost(e) for e in context) - 1 if context else 0)

    new: list[dict] = []
    took_id = False
    for entry in spine[start:end]:
        cost = _cost(entry)
        # "At least one" means at least one entry that can carry the marker: a
        # part of only user entries or id-less tool entries could never move it.
        if took_id and cost > left:
            break
        new.append(entry)
        left -= cost
        took_id = took_id or _tool_id(entry) is not None
    held_back = (end - start) - len(new)

    through_id = marker if not reset else None
    for entry in new:
        through_id = _tool_id(entry) or through_id
    return Part(
        new=new,
        context=context,
        through_id=through_id,
        reset=reset,
        pending_tail=pending_tail,
        held_back=held_back,
    )
