"""Presentation selection cannot discard contrary usage or evidence."""

import json

import pytest

from genesis.transcript_analytics import query, store


def _record(uuid, tokens, *, terminal=True, text="block", sid="parent", provider="turn"):
    return {
        "type": "assistant",
        "sessionId": sid,
        "uuid": uuid,
        "timestamp": "2026-10-07T00:00:00Z",
        "message": {
            "id": provider,
            "model": "model",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn" if terminal else None,
            "usage": {"output_tokens": tokens},
        },
    }


def _turn(tmp_path, files):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    for name, records in files.items():
        (projects / name).write_text("".join(json.dumps(record) + "\n" for record in records))
    assert store.ingest(projects, data, lock_path=tmp_path / "writer.lock")["failed"] == 0
    columns, rows = query.run_query(data, "SELECT * FROM turns", live=True)
    assert len(rows) == 1
    return dict(zip(columns, rows[0], strict=True))


def test_different_terminal_uuid_assertions_cannot_choose_last_usage(tmp_path):
    turn = _turn(
        tmp_path,
        {
            "a.jsonl": [
                _record("A", 10, text="first"),
                _record("B", 20, text="second"),
            ]
        },
    )
    assert turn["usage_available"] is False
    assert turn["output_tokens"] is None
    assert turn["final_usage_conflict"] is True


def test_equal_terminal_state_across_distinct_blocks_is_one_vector(tmp_path):
    turn = _turn(
        tmp_path,
        {
            "a.jsonl": [
                _record("A", 10, text="first"),
                _record("B", 10, text="second"),
            ]
        },
    )
    assert turn["usage_available"] is True
    assert turn["output_tokens"] == 10
    assert turn["n_records"] == 2


@pytest.mark.parametrize(
    "field",
    [
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "ephemeral_5m_input_tokens",
        "ephemeral_1h_input_tokens",
        "thinking_tokens",
    ],
)
def test_every_usage_field_participates_in_terminal_agreement(tmp_path, field):
    first, second = _record("A", 10), _record("B", 10)
    for record, value in [(first, 1), (second, 2)]:
        usage = record["message"]["usage"]
        if field.startswith("ephemeral_"):
            usage["cache_creation"] = {field: value}
        elif field == "thinking_tokens":
            usage["output_tokens_details"] = {field: value}
        else:
            usage[field] = value
    turn = _turn(tmp_path, {"a.jsonl": [first, second]})
    assert turn["usage_available"] is False
    assert turn["final_usage_conflict"] is True
    assert turn["output_tokens"] is None


def test_cumulative_partial_state_is_not_summed_as_terminal(tmp_path):
    turn = _turn(
        tmp_path,
        {
            "a.jsonl": [
                _record("A", 3, terminal=False),
                _record("B", 10),
            ]
        },
    )
    assert turn["usage_available"] is True
    assert turn["output_tokens"] == 10


@pytest.mark.parametrize("request_value", ["", "request", 0, False, [], {}])
@pytest.mark.parametrize("reverse", [False, True])
def test_missing_and_present_request_state_conflicts_in_either_order(
    tmp_path, request_value, reverse
):
    first, second = _record("A", 10), _record("A", 10)
    second["requestId"] = request_value
    records = [second, first] if reverse else [first, second]
    turn = _turn(tmp_path, {"a.jsonl": [records[0]], "b.jsonl": [records[1]]})
    assert turn["usage_available"] is False
    assert turn["final_usage_conflict"] is True


def test_null_request_and_missing_request_share_unknown_state(tmp_path):
    first, second = _record("A", 10), _record("A", 10)
    second["requestId"] = None
    turn = _turn(tmp_path, {"a.jsonl": [first], "b.jsonl": [second]})
    assert turn["usage_available"] is True
    assert turn["output_tokens"] == 10


def test_early_event_state_conflict_is_not_erased_by_clean_final(tmp_path):
    turn = _turn(
        tmp_path,
        {
            "a.jsonl": [_record("A", 3, terminal=False), _record("B", 10)],
            "b.jsonl": [_record("A", 5, terminal=False), _record("B", 10)],
        },
    )
    assert turn["usage_available"] is False
    assert turn["final_usage_conflict"] is True
    assert turn["output_tokens"] is None


def test_divergent_sequence_preserves_all_evidence_and_contexts(tmp_path):
    turn = _turn(
        tmp_path,
        {
            "a.jsonl": [_record("A", 3, terminal=False), _record("B", 10)],
            "b.jsonl": [_record("A", 3, terminal=False), _record("C", 10, sid="other")],
        },
    )
    assert turn["assembly_complete"] is False
    assert {(r["source_file"], r["line_no"]) for r in turn["source_references"]} == {
        ("a.jsonl", 1),
        ("a.jsonl", 2),
        ("b.jsonl", 1),
        ("b.jsonl", 2),
    }
    assert set(turn["context_sessions"]) == {"parent", "other"}
    assert turn["distinct_event_count"] == 3
    assert turn["physical_observation_count"] == 4
    assert turn["attribution_status"] == "unresolved"


@pytest.mark.parametrize("provider", [None, ""])
def test_same_uuid_missing_and_present_provider_forms_one_event_bucket(tmp_path, provider):
    turn = _turn(
        tmp_path,
        {
            "a.jsonl": [_record("A", 10)],
            "b.jsonl": [_record("A", 10, provider=provider)],
        },
    )
    assert turn["n_records"] == 1
    assert turn["distinct_event_count"] == 1
    assert turn["physical_observation_count"] == 2
    assert turn["usage_available"] is False


@pytest.mark.parametrize("same_event", [False, True])
@pytest.mark.parametrize("terminal_error", [False, True])
@pytest.mark.parametrize("synthetic", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_api_error_terminal_assertion_participates_before_usage_selection(
    tmp_path, same_event, terminal_error, synthetic, reverse
):
    clean = _record("A", 7, text="clean")
    error = _record("A" if same_event else "B", 7, terminal=terminal_error, text="API error")
    error.update(isApiErrorMessage=True, apiErrorStatus=429)
    usage = {
        "input_tokens": 1,
        "output_tokens": 7,
        "cache_read_input_tokens": 2,
        "cache_creation_input_tokens": 3,
        "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 5},
        "output_tokens_details": {"thinking_tokens": 6},
    }
    clean["message"]["usage"] = usage
    error["message"]["usage"] = usage
    if synthetic:
        error["message"]["model"] = "<synthetic>"
    items = [error, clean] if reverse else [clean, error]
    turn = _turn(tmp_path, {"source.jsonl": items})
    conflicting = terminal_error or same_event
    assert turn["usage_available"] is (not conflicting)
    assert turn["final_usage_conflict"] is conflicting
    assert turn["output_tokens"] == (None if conflicting else 7)
    for field, expected in {
        "input_tokens": 1,
        "output_tokens": 7,
        "cache_read": 2,
        "cache_create": 3,
        "cache_create_5m": 4,
        "cache_create_1h": 5,
        "thinking_tokens": 6,
    }.items():
        assert turn[field] == (None if conflicting else expected)
    assert turn["physical_observation_count"] == 2
    if terminal_error:
        assert turn["terminal_variants"] == 2
    assert turn["is_api_error"] is True


@pytest.mark.parametrize("flag", ["api_error", "scrub_failed"])
@pytest.mark.parametrize("same_source", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_nonselected_copy_failure_flags_survive_in_turn_and_session(
    tmp_path, flag, same_source, reverse
):
    clean, failed = _record("A", 7), _record("A", 7)
    if flag == "api_error":
        failed.update(isApiErrorMessage=True, apiErrorStatus=429)
    else:
        failed["message"]["model"] = '{"password":'
    items = [failed, clean] if reverse else [clean, failed]
    files = {"a.jsonl": items} if same_source else {"a.jsonl": [items[0]], "b.jsonl": [items[1]]}
    turn = _turn(tmp_path, files)
    assert turn["is_api_error"] is (flag == "api_error")
    assert turn["scrub_failed"] is (flag == "scrub_failed")
    assert turn["physical_observation_count"] == 2
    assert len(turn["source_references"]) == 2
    assert turn["usage_available"] is False
    _columns, sessions = query.run_query(
        tmp_path / "data", "SELECT scrub_failed FROM sessions", live=True
    )
    assert sessions == [(flag == "scrub_failed",)]
