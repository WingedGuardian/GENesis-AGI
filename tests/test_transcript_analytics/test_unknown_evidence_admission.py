"""Unknown evidence never becomes verified content or supported attribution."""

import contextlib
import itertools

import pytest
from test_deterministic_views import _connection, _rows

from genesis.transcript_analytics.extract import TABLES

HASHES = ((None,), ("a",), ("a", "a"), ("a", None), ("a", "b"), ("a", "b", None))
ACTOR = '["s",null]'


def _state(values):
    known = {value for value in values if value is not None}
    return True if len(known) > 1 else None if None in values else False


def _empty():
    return {name: [] for name in TABLES}


@pytest.mark.parametrize("table", ["hooks", "events"])
@pytest.mark.parametrize(
    "actors", [(ACTOR,), (None,), (ACTOR, None), (ACTOR, ACTOR), (ACTOR, '["s","child"]')]
)
@pytest.mark.parametrize("context_conflict", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_source_attribution_admits_the_whole_evidence_envelope(
    table, actors, context_conflict, reverse
):
    rows = _empty()
    expected = []
    for index, hashes in enumerate(HASHES):
        copies = list(itertools.product(hashes, actors))
        rows[table].extend(
            {
                "source_file": f"event-{index}-copy-{copy}.jsonl",
                "line_no": 1,
                "uuid": str(index),
                "record_hash": value,
                "actor_id": actor,
                "context_session_id": "s",
                "context_conflict": context_conflict,
                "scrub_failed": copy == len(copies) - 1,
            }
            for copy, (value, actor) in enumerate(copies)
        )
        state = _state(hashes)
        supported = (
            state is False
            and actors.count(None) == 0
            and len(set(actors)) == 1
            and not context_conflict
        )
        expected.append(
            (
                str(index),
                state,
                ACTOR if supported else None,
                "conflicting"
                if state is True or context_conflict
                else "confirmed"
                if supported
                else "unresolved",
                True,
                len(copies),
            )
        )
    with contextlib.closing(_connection(rows, reverse)) as con:
        assert (
            con.sql(
                f"SELECT uuid,content_conflict,executor_id,attribution_status,scrub_failed,len(source_references) FROM {table} ORDER BY uuid"
            ).fetchall()
            == expected
        )
        included = sum(_state(hashes) is False for hashes in HASHES)
        assert con.sql(
            "SELECT included,excluded FROM metric_coverage WHERE metric=?",
            params=["hook_content" if table == "hooks" else "event_content"],
        ).fetchone() == (included, len(HASHES) - included)


@pytest.mark.parametrize("inferred", [False, True])
@pytest.mark.parametrize("context_conflict", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_event_ownership_status_and_reason_follow_admitted_executor(
    inferred, context_conflict, reverse
):
    rows = _empty()
    expected = []
    for index, hashes in enumerate(HASHES):
        template = _rows()["fragments"][0]
        for copy, value in enumerate(hashes):
            fragment = {
                **template,
                "source_file": f"main-{index}-{copy}.jsonl",
                "uuid": f"event-{index}",
                "message_id": f"message-{index}",
                "content_hash": value,
                "context_conflict": context_conflict,
            }
            rows["fragments"].append(fragment)
            if inferred:
                rows["fragments"].append(
                    {
                        **fragment,
                        "source_file": f"child-{index}-{copy}.jsonl",
                        "source_role": "child",
                        "actor_id": '["s","child"]',
                    }
                )
        state = _state(hashes)
        supported = state is False and not context_conflict
        expected.append(
            (
                f"uuid:event-{index}",
                ACTOR if supported else None,
                "conflicting"
                if state is True or context_conflict
                else ("inferred" if inferred else "confirmed")
                if supported
                else "unresolved",
            )
        )
    with contextlib.closing(_connection(rows, reverse)) as con:
        assert (
            con.sql(
                "SELECT event_key,executor_id,attribution_status FROM event_ownership ORDER BY event_key"
            ).fetchall()
            == expected
        )
        assert con.sql(
            "SELECT count(*) FROM event_ownership WHERE executor_id IS NULL AND attribution_status IN ('confirmed','inferred')"
        ).fetchone() == (0,)
        assert con.sql(
            "SELECT count(*) FROM event_ownership WHERE event_key IN ('uuid:event-0','uuid:event-3') AND attribution_reason NOT LIKE '%unknown%' AND NOT context_conflict"
        ).fetchone() == (0,)


@pytest.mark.parametrize("placement", ["paired", "call-only", "result-only"])
@pytest.mark.parametrize("layout", ["same-source", "copies"])
@pytest.mark.parametrize("reverse", [False, True])
def test_tool_envelopes_preserve_all_hash_states_and_absence(placement, layout, reverse):
    rows = _empty()
    expected = []
    cases = (
        itertools.product(HASHES, HASHES)
        if placement == "paired"
        else ((hashes, hashes) for hashes in HASHES)
    )
    for index, (calls, results) in enumerate(cases):
        # Orphans in different sources are different evidence envelopes; only
        # same-source variants share an orphan identity.
        copies = list(itertools.product(calls, results))
        source_copies = layout == "copies" and placement != "result-only"
        sources = [
            f"case-{index}-copy-{copy if source_copies else 0}.jsonl" for copy in range(len(copies))
        ]
        template = {
            **_rows()["fragments"][0],
            "uuid": f"call-{index}",
            "message_id": f"message-{index}",
        }
        for copy, ((call_hash, result_hash), source) in enumerate(
            zip(copies, sources, strict=True)
        ):
            if placement != "result-only":
                rows["fragments"].append({**template, "source_file": source})
            rows["tool_calls"].append(
                {
                    "source_file": source,
                    "session_id": "s",
                    "context_session_id": "s",
                    "actor_id": ACTOR,
                    "context_conflict": False,
                    "source_role": "main",
                    "tool_use_id": f"tool-{index}",
                    "call_uuid": template["uuid"] if placement != "result-only" else None,
                    "message_id": template["message_id"] if placement != "result-only" else None,
                    "line_no_call": 1 if placement != "result-only" else None,
                    "line_no_result": 2 if placement != "call-only" else None,
                    "call_hash": call_hash if placement != "result-only" else None,
                    "result_hash": result_hash if placement != "call-only" else None,
                    "tool": "Bash",
                    "scrub_failed": copy == len(copies) - 1,
                    "has_result": placement != "call-only",
                }
            )
        call_state = _state(calls) if placement != "result-only" else False
        result_state = _state(results) if placement != "call-only" else False
        owner = ACTOR if placement != "result-only" and call_state is False else None
        status = (
            None
            if placement == "result-only"
            else "confirmed"
            if owner
            else "conflicting"
            if call_state is True
            else "unresolved"
        )
        expected.append(
            (
                f"tool-{index}",
                call_state,
                result_state,
                placement != "result-only" and call_state is False,
                placement != "call-only" and result_state is False,
                owner,
                status,
                True,
                len(set(sources)),
            )
        )
    with contextlib.closing(_connection(rows, reverse)) as con:
        observed = con.sql(
            "SELECT tool_use_id,content_conflict,result_conflict,call_valid,result_valid,executor_id,attribution_status,scrub_failed,len(source_references) FROM tool_calls ORDER BY tool_use_id"
        ).fetchall()
        assert observed == sorted(expected)
        for metric, column in (("tool_call_content", 3), ("tool_result_content", 4)):
            included = sum(row[column] for row in expected)
            assert con.sql(
                "SELECT included,excluded FROM metric_coverage WHERE metric=?", params=[metric]
            ).fetchone() == (included, len(expected) - included)
        assert con.sql(
            "SELECT count(*) FROM tool_ownership WHERE executor_id IS NULL AND attribution_status IN ('confirmed','inferred')"
        ).fetchone() == (0,)
        assert con.sql(
            "SELECT count(*) FROM tool_calls WHERE call_valid IS NULL OR result_valid IS NULL"
        ).fetchone() == (0,)


@pytest.mark.parametrize("route", ["unpaired-result", "companion", "explicit-companion"])
@pytest.mark.parametrize("hashes", HASHES)
@pytest.mark.parametrize("reverse", [False, True])
def test_every_lineage_consumer_uses_global_call_admission(route, hashes, reverse):
    rows = _empty()
    actor = '["s","parent"]' if route == "explicit-companion" else ACTOR
    template = {
        **_rows()["fragments"][0],
        "actor_id": actor,
        "source_role": "child" if route == "explicit-companion" else "main",
    }
    for index, value in enumerate(hashes):
        source = f"copy-{index}.jsonl"
        rows["fragments"].append({**template, "source_file": source})
        rows["tool_calls"].append(
            {
                "source_file": source,
                "session_id": "s",
                "context_session_id": "s",
                "actor_id": actor,
                "context_conflict": False,
                "source_role": "main",
                "tool_use_id": "spawn",
                "call_uuid": template["uuid"],
                "message_id": template["message_id"],
                "line_no_call": 1,
                "call_hash": value,
                "tool": "Agent",
                "scrub_failed": False,
                "has_result": False,
            }
        )
    if route == "unpaired-result":
        rows["tool_calls"].append(
            {
                "source_file": "result.jsonl",
                "session_id": "s",
                "context_session_id": "s",
                "actor_id": ACTOR,
                "context_conflict": False,
                "source_role": "main",
                "tool_use_id": "spawn",
                "line_no_result": 2,
                "result_hash": "result",
                "result_agent_ids": ["child"],
                "scrub_failed": False,
                "has_result": True,
            }
        )
    else:
        rows["agents"].append(
            {
                "source_file": "child.jsonl",
                "context_session_id": "s",
                "actor_id": '["s","child"]',
                "tool_use_id": "spawn",
                "parent_agent_id": "parent" if route == "explicit-companion" else None,
            }
        )
    rows["fragments"].append(
        {
            **_rows()["fragments"][1],
            "source_file": "child.jsonl",
            "source_role": "child",
            "actor_id": '["s","child"]',
            "agent_id": "child",
            "uuid": "child-event",
            "message_id": "child-message",
            "content_hash": "child-content",
            "payload_hash": "child-payload",
        }
    )
    state = _state(hashes)
    supported = state is False or (state is None and route == "explicit-companion")
    with contextlib.closing(_connection(rows, reverse)) as con:
        observed = con.sql(
            "SELECT parent_actor_id,lineage_status FROM actor_lineage WHERE actor_id=?",
            params=['["s","child"]'],
        ).fetchall()
        if supported:
            assert observed == [(actor, "confirmed")]
        else:
            assert all(
                parent is None and status not in ("confirmed", "inferred")
                for parent, status in observed
            )
            assert observed, "The child evidence must remain available as unresolved/conflicting"
        assert con.sql(
            "SELECT n_turns_inclusive FROM delegation_rollups WHERE actor_id=?", params=[actor]
        ).fetchone() == (2 if supported else 1,)
        assert con.sql(
            "SELECT n_turns_inclusive FROM delegation_rollups WHERE actor_id=?",
            params=['["s","child"]'],
        ).fetchone() == (1,)
        assert con.sql(
            "SELECT included,excluded FROM metric_coverage WHERE metric='spawn_association'"
        ).fetchone() == (int(supported), int(not supported))
        expected_status = (
            "conflicting" if state is True else "unresolved" if state is None else "confirmed"
        )
        assert con.sql(
            "SELECT DISTINCT executor_id,attribution_status FROM tool_ownership"
        ).fetchall() == [(actor if state is False else None, expected_status)]


@pytest.mark.parametrize("hashes,conflicting", [(("a", "b"), True), (("a", "a"), False)])
@pytest.mark.parametrize("reverse", [False, True])
def test_distinct_events_preserve_existing_source_local_hash_guard(hashes, conflicting, reverse):
    rows = _empty()
    template = _rows()["fragments"][0]
    for index, value in enumerate(hashes):
        fragment = {
            **template,
            "source_file": "one.jsonl",
            "uuid": f"event-{index}",
            "message_id": f"message-{index}",
            "line_no": index + 1,
        }
        rows["fragments"].append(fragment)
        rows["tool_calls"].append(
            {
                "source_file": "one.jsonl",
                "session_id": "s",
                "context_session_id": "s",
                "actor_id": ACTOR,
                "context_conflict": False,
                "source_role": "main",
                "tool_use_id": "reused-id",
                "call_uuid": fragment["uuid"],
                "message_id": fragment["message_id"],
                "line_no_call": index + 1,
                "call_hash": value,
                "tool": "Agent",
                "scrub_failed": False,
                "has_result": False,
            }
        )
    with contextlib.closing(_connection(rows, reverse)) as con:
        assert con.sql("SELECT executor_id,attribution_status FROM tool_ownership").fetchall() == [
            (None, "conflicting") if conflicting else (ACTOR, "confirmed")
        ]
        # Distinct event envelopes remain distinct; this preserves the earlier
        # source-local ownership constraint rather than conflating call identity.
        assert con.sql(
            "SELECT count(*),bool_or(content_conflict),bool_and(call_valid) FROM tool_calls"
        ).fetchone() == (2, False, True)
