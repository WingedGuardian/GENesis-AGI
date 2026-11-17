"""select_part: the part of a session's spine the lesson builder has not judged (#4807)."""

from __future__ import annotations

from genesis.learning.procedural.build_segment import select_part
from genesis.learning.procedural.struggle_detector import judge_line


def _tool(n, *, result=True, tid=None, args="a"):
    return {
        "turn": n,
        "type": "tool",
        "tool": "Bash",
        "args_summary": args,
        "outcome": "ok" if result else "error",
        "error_text": "" if result else "no result received (session may have crashed)",
        "tool_use_id": f"t{n}" if tid is None else tid,
        "has_result": result,
    }


def _user(n, text="please"):
    return {
        "turn": n,
        "type": "user",
        "tool": None,
        "args_summary": text,
        "outcome": "ok",
        "error_text": "",
    }


def _turns(part):
    return [e["turn"] for e in part.new]


BIG = 1_000_000


def test_no_marker_takes_the_whole_spine_without_a_reset():
    spine = [_tool(1), _user(2), _tool(3)]
    part = select_part(spine, None, budget_chars=BIG)
    assert _turns(part) == [1, 2, 3]
    assert part.context == [] and not part.reset and part.through_id == "t3"


def test_a_marker_mid_spine_takes_only_what_follows_it():
    spine = [_tool(1), _tool(2), _user(3), _tool(4)]
    part = select_part(spine, "t2", budget_chars=BIG)
    assert _turns(part) == [3, 4]  # user entries count: the correction signal
    assert [e["turn"] for e in part.context] == [1, 2]
    assert part.through_id == "t4" and not part.reset


def test_a_marker_at_the_end_leaves_nothing_new():
    part = select_part([_tool(1), _tool(2)], "t2", budget_chars=BIG)
    assert part.new == [] and part.through_id == "t2"


def test_a_marker_not_in_the_spine_resets_to_the_start():
    """The file was replaced: judge it from the start, once."""
    part = select_part([_tool(1), _tool(2)], "gone", budget_chars=BIG)
    assert part.reset and _turns(part) == [1, 2] and part.context == []


def test_a_trailing_call_in_flight_waits():
    spine = [_tool(1), _tool(2, result=False), _tool(3, result=False)]
    part = select_part(spine, None, budget_chars=BIG)
    assert _turns(part) == [1] and part.pending_tail == 2 and part.through_id == "t1"


def test_a_call_without_a_result_before_a_user_turn_is_judged():
    """A user turn after it means the call was cut short (a crash), not running."""
    spine = [_tool(1, result=False), _user(2), _tool(3)]
    part = select_part(spine, None, budget_chars=BIG)
    assert _turns(part) == [1, 2, 3] and part.pending_tail == 0


def test_a_parallel_batch_waits_while_any_call_in_it_is_unanswered():
    """Results of parallel calls can be written out of order: C's result
    before A's and B's leaves A and B unanswered but not trailing. They may
    still be running, so the batch waits rather than being judged as crashed."""
    spine = [_user(1), _tool(2, result=False), _tool(3, result=False), _tool(4)]
    part = select_part(spine, None, budget_chars=BIG)
    assert _turns(part) == [1] and part.pending_tail == 3


def test_a_user_entry_after_an_unanswered_call_ends_the_tail():
    spine = [_tool(1, result=False), _user(2)]
    part = select_part(spine, None, budget_chars=BIG)
    assert _turns(part) == [1, 2] and part.pending_tail == 0


def test_position_not_result_time_decides_the_part():
    """Result stamps are not monotonic; the part is cut by spine order only."""
    spine = [_tool(1), _tool(2), _tool(3)]
    spine[0]["timestamp"] = "2026-11-15T12:00:09Z"
    spine[1]["timestamp"] = "2026-11-15T12:00:01Z"
    part = select_part(spine, "t1", budget_chars=BIG)
    assert _turns(part) == [2, 3]


def test_the_budget_takes_the_oldest_new_entries_that_fit():
    spine = [_tool(i, args="x" * 50) for i in range(1, 11)]
    per_line = len(judge_line(spine[0])) + 1
    part = select_part(spine, None, budget_chars=per_line * 3)
    assert _turns(part) == [1, 2, 3]
    assert part.held_back == 7 and part.through_id == "t3"


def test_the_context_comes_out_of_the_budget_first():
    spine = [_tool(i, args="x" * 50) for i in range(1, 11)]
    per_line = len(judge_line(spine[0])) + 1
    part = select_part(spine, "t4", budget_chars=per_line * 5, context_n=2)
    assert [e["turn"] for e in part.context] == [3, 4]
    assert _turns(part) == [5, 6, 7]


def test_the_context_is_tool_entries_only_and_bounded():
    """The judge's view drops user turns, so context counts tool entries."""
    spine = [_tool(1), _tool(2), _user(3), _tool(4), _user(5), _tool(6)]
    part = select_part(spine, "t4", budget_chars=BIG, context_n=2)
    assert [e["turn"] for e in part.context] == [2, 4]
    assert _turns(part) == [5, 6]


def test_a_repeated_id_cuts_after_its_first_occurrence():
    """A transcript can replay earlier calls (api-request-blob records), so an id
    can appear twice. Cutting at the first copy can only re-judge; cutting at
    the last could skip everything between the two copies unjudged."""
    spine = [_tool(1), _tool(2, tid="dup"), _tool(3), _tool(4, tid="dup"), _tool(5)]
    part = select_part(spine, "dup", budget_chars=BIG)
    assert _turns(part) == [3, 4, 5]


def test_at_least_one_entry_is_taken_when_the_first_exceeds_the_budget():
    """Otherwise one oversized call would hold the marker forever."""
    spine = [_tool(1, args="x" * 500), _tool(2)]
    part = select_part(spine, None, budget_chars=10)
    assert _turns(part) == [1] and part.through_id == "t1"


def test_entries_without_an_id_never_become_the_marker():
    spine = [_tool(1), _tool(2, tid="")]
    spine[1]["tool_use_id"] = None
    part = select_part(spine, None, budget_chars=BIG)
    assert _turns(part) == [1, 2] and part.through_id == "t1"


def test_a_part_with_no_marker_able_entry_keeps_the_old_marker():
    part = select_part([_tool(1), _user(2)], "t1", budget_chars=BIG)
    assert _turns(part) == [2] and part.through_id == "t1"


def test_an_empty_spine():
    part = select_part([], None, budget_chars=BIG)
    assert part.new == [] and part.through_id is None and not part.reset
