"""Identity, state and ownership have separate evidence and denominators."""

import copy
import json
import os

from genesis.transcript_analytics import derive, query, store


def assistant(uuid="u", mid="m", *, sid="s", stop="end_turn", out=7, content=None):
    return {
        "type": "assistant",
        "uuid": uuid,
        "sessionId": sid,
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {
            "id": mid,
            "model": "model",
            "content": content or [{"type": "text", "text": "x"}],
            "stop_reason": stop,
            "usage": {"input_tokens": 2, "output_tokens": out},
        },
    }


def put(root, rel, rows, meta=None):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    if meta is not None:
        path.with_suffix(".meta.json").write_text(json.dumps(meta))
    return path


def built(tmp_path, files):
    root, data = tmp_path / "projects", tmp_path / "data"
    for rel, rows in files.items():
        put(root, rel, rows)
    assert store.ingest(root, data)["failed"] == 0
    return root, data


def rows(data, sql):
    return query.run_query(data, sql, live=True)[1]


def test_terminal_upgrade_selects_whole_record_independent_of_copy_size(tmp_path):
    partial = assistant(stop=None, out=99)
    terminal = assistant(out=4)
    terminal["message"]["usage"]["extra"] = {"nested": 3}
    root, data = built(
        tmp_path, {"p/a.jsonl": [partial, assistant("u2", "other")], "p/z.jsonl": [terminal]}
    )
    assert rows(
        data, "select output_tokens,usage_available,n_records from turns where message_id='m'"
    ) == [(4, True, 1)]
    assert rows(
        data, "select len(source_references),attribution_status from turns where message_id='m'"
    ) == [(2, "confirmed")]


def test_complete_usage_variant_conflicts_instead_of_maximum(tmp_path):
    a = assistant(out=7)
    b = copy.deepcopy(a)
    b["message"]["usage"]["unindexed_usage"] = {"billed": 1}
    _, data = built(tmp_path, {"p/a.jsonl": [a], "p/z.jsonl": [b]})
    assert rows(data, "select count(*),sum(output_tokens) from turns") == [(1, None)]
    assert rows(
        data, "select usage_available,final_usage_conflict,content_conflict from turns"
    ) == [(False, True, False)]
    assert rows(
        data, "select included,excluded from metric_coverage where metric='turn_usage'"
    ) == [(0, 1)]


def test_different_content_same_identity_is_visible_once_and_excluded(tmp_path):
    _, data = built(
        tmp_path,
        {
            "a.jsonl": [assistant()],
            "b.jsonl": [assistant(content=[{"type": "text", "text": "different"}])],
        },
    )
    assert rows(
        data, "select count(*),bool_or(content_conflict),sum(output_tokens) from turns"
    ) == [(1, True, None)]
    assert rows(data, "select attribution_status from turns") == [("conflicting",)]


def test_sequence_dominance_and_conflict_do_not_stitch(tmp_path):
    a, b = assistant("u1", stop=None), assistant("u2", out=5)
    _, data = built(tmp_path, {"a.jsonl": [a, b], "b.jsonl": [b]})
    assert rows(data, "select n_records,assembly_complete,output_tokens from turns") == [
        (2, True, 5)
    ]
    put(tmp_path / "projects", "b.jsonl", [b, a])
    store.ingest(tmp_path / "projects", data)
    assert rows(data, "select n_records,assembly_complete,output_tokens from turns") == [
        (2, False, None)
    ]


def test_missing_uuid_is_source_local_even_provider_id_matches(tmp_path):
    a = assistant()
    a.pop("uuid")
    _, data = built(tmp_path, {"a.jsonl": [a], "b.jsonl": [a]})
    assert rows(data, "select count(*) from turns") == [(2,)]


def test_cross_session_copy_no_chronological_owner(tmp_path):
    a = assistant(sid="first")
    b = assistant(sid="second")
    _, data = built(tmp_path, {"z.jsonl": [a], "a.jsonl": [b]})
    assert rows(data, "select count(*),max(executor_id),max(session_id) from turns") == [
        (1, None, None)
    ]
    assert rows(data, "select attribution_status,len(context_sessions) from turns") == [
        ("unresolved", 2)
    ]
    assert rows(data, "select sum(n_turns_inclusive) from context_rollups") == [(2,)]


def test_nested_child_prefix_inference_and_spawn_producer(tmp_path):
    spawn = assistant(
        "spawn", "spawn", content=[{"type": "tool_use", "id": "task", "name": "Agent", "input": {}}]
    )
    child = assistant("child", "child")
    root, data = built(
        tmp_path, {"p/s.jsonl": [spawn], "p/s/subagents/workflows/w/agent-c.jsonl": [spawn, child]}
    )
    put(
        root,
        "p/s/subagents/workflows/w/agent-c.jsonl",
        [spawn, child],
        {"toolUseId": "task", "parentAgentId": None},
    )
    store.ingest(root, data)
    assert rows(
        data, "select attribution_status,executor_id from turns where message_id='spawn'"
    ) == [("inferred", '["s",null]')]
    assert rows(
        data, "select executor_id,delegator_id,lineage_status from turns where message_id='child'"
    ) == [('["s","c"]', '["s",null]', "inferred")]
    assert rows(
        data, "select n_turns_inclusive from delegation_rollups where actor_id='[\"s\",null]'"
    ) == [(2,)]
    assert rows(data, "select sum(n_turns) from executors") == [(2,)]


def test_explicit_parent_conflict_is_not_overruled_by_spawn(tmp_path):
    spawn = assistant(content=[{"type": "tool_use", "id": "task", "name": "Task", "input": {}}])
    root, data = built(
        tmp_path, {"p/s.jsonl": [spawn], "p/s/subagents/agent-c.jsonl": [assistant("c", "child")]}
    )
    put(
        root,
        "p/s/subagents/agent-c.jsonl",
        [assistant("c", "child")],
        {"toolUseId": "task", "parentAgentId": "other"},
    )
    store.ingest(root, data)
    assert rows(
        data,
        'select lineage_status,parent_actor_id from actor_lineage where actor_id=\'["s","c"]\'',
    ) == [("conflicting", None)]


def test_since_repairs_known_old_generation_but_skips_new_old_file(tmp_path, monkeypatch):
    root, data = built(tmp_path, {"a.jsonl": [assistant()]})
    os.utime(root / "a.jsonl", (1, 1))
    put(root, "new.jsonl", [assistant("new", "new")])
    os.utime(root / "new.jsonl", (1, 1))
    monkeypatch.setattr(store, "SCHEMA_VERSION", "test-new-semantics")
    summary = store.ingest(root, data, since_days=1)
    assert summary["rebuilt"] == 1 and summary["skipped_old"] == 1
    assert store.compatible_sources(data)[1] == []


def test_companion_changed_rebuilds_and_snapshot_preserves_new_views(tmp_path):
    root, data = built(tmp_path, {"p/s/subagents/agent-c.jsonl": [assistant()]})
    put(root, "p/s/subagents/agent-c.jsonl", [assistant()], {"parentAgentId": "x"})
    assert store.ingest(root, data)["rebuilt"] == 1
    derive.build(data)
    assert query.snapshot_compatible(data)
    for view in (
        "actor_lineage",
        "executors",
        "delegation_rollups",
        "context_rollups",
        "metric_coverage",
    ):
        assert query.run_query(data, f"select * from {view}")[1] == rows(
            data, f"select * from {view}"
        )


def test_duplicate_hook_event_variants_keep_all_refs_and_conflict_coverage(tmp_path):
    hook = {
        "type": "attachment",
        "uuid": "hook",
        "sessionId": "s",
        "timestamp": "2026-01-01T00:00:00Z",
        "attachment": {"type": "hook_result", "command": "one"},
    }
    event = {
        "type": "system",
        "subtype": "turn_duration",
        "uuid": "event",
        "sessionId": "s",
        "timestamp": "2026-01-01T00:00:00Z",
        "durationMs": 1,
    }
    changed_hook = copy.deepcopy(hook)
    changed_hook["attachment"]["command"] = "two"
    changed_event = copy.deepcopy(event)
    changed_event["durationMs"] = 2
    _, data = built(tmp_path, {"a.jsonl": [hook, event], "b.jsonl": [changed_hook, changed_event]})
    assert rows(
        data, "select count(*),bool_or(content_conflict),max(len(source_references)) from hooks"
    ) == [(1, True, 2)]
    assert rows(
        data, "select count(*),bool_or(content_conflict),max(len(source_references)) from events"
    ) == [(1, True, 2)]
    assert rows(
        data, "select included,excluded from metric_coverage where metric='event_content'"
    ) == [(0, 1)]


def test_fractional_and_offset_timestamps_order_as_instants(tmp_path):
    a = assistant("early", "m", stop=None)
    a["timestamp"] = "2026-01-01T01:00:00+01:00"
    b = assistant("late", "m")
    b["timestamp"] = "2026-01-01T00:00:00.100Z"
    _, data = built(tmp_path, {"a.jsonl": [a, b]})
    assert rows(data, "select ts,ts_last from turns") == [
        ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00.100000Z")
    ]


def test_duplicate_in_source_call_and_result_payload_conflicts_survive(tmp_path):
    call = assistant(
        content=[{"type": "tool_use", "id": "task", "name": "Bash", "input": {"command": "one"}}]
    )
    changed = copy.deepcopy(call)
    changed["message"]["content"][0]["input"]["command"] = "two"
    result = {
        "type": "user",
        "sessionId": "s",
        "timestamp": "2026-01-01T00:00:01Z",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "task", "content": "ok"}]},
    }
    result2 = copy.deepcopy(result)
    result2["message"]["content"][0]["content"] = "different"
    _, data = built(tmp_path, {"a.jsonl": [call, changed, result, result2]})
    assert rows(
        data,
        "select count(*),bool_or(content_conflict),bool_or(result_conflict),bool_or(call_valid),bool_or(result_valid) from tool_calls",
    ) == [(1, True, True, False, False)]
    assert rows(data, "select count(*) from raw_tool_calls") == [(3,)]


def test_request_conflicts_prevent_terminal_upgrade(tmp_path):
    a = assistant(stop=None)
    a["requestId"] = "one"
    b = assistant()
    b["requestId"] = "two"
    _, data = built(tmp_path, {"a.jsonl": [a], "b.jsonl": [b]})
    assert rows(data, "select usage_available,final_usage_conflict from turns") == [(False, True)]


def test_parent_cycle_and_unicode_actor_contract():
    from genesis.transcript_analytics.extract import actor_identity
    from genesis.transcript_analytics.identity import encode_identity
    from genesis.transcript_analytics.reconcile import _parent_graph

    result = _parent_graph(
        [
            ("a", "b", "confirmed", "companion toolUseId", "src"),
            ("b", "a", "confirmed", "Agent/Task result", "src"),
        ]
    )
    assert all(
        row["lineage_status"] == "conflicting" and row["parent_actor_id"] is None for row in result
    )
    actor = actor_identity("jsonid:context", "\ud800")
    assert json.loads(actor) == [encode_identity("jsonid:context"), encode_identity("\ud800")]


def test_explicit_parent_narrows_matching_actual_spawn_with_ambiguous_copies(tmp_path):
    spawn = assistant(content=[{"type": "tool_use", "id": "spawn", "name": "Agent", "input": {}}])
    root, data = built(
        tmp_path,
        {
            "p/s/subagents/agent-parent.jsonl": [spawn],
            "p/s/subagents/agent-copy.jsonl": [spawn],
            "p/s/subagents/agent-child.jsonl": [assistant("child", "child")],
        },
    )
    put(
        root,
        "p/s/subagents/agent-child.jsonl",
        [assistant("child", "child")],
        {"parentAgentId": "parent", "toolUseId": "spawn"},
    )
    store.ingest(root, data)
    assert rows(data, "select executor_id,attribution_status from turns where message_id='m'") == [
        (None, "unresolved")
    ]
    assert rows(
        data,
        'select parent_actor_id,lineage_status from actor_lineage where actor_id=\'["s","child"]\'',
    ) == [('["s","parent"]', "confirmed")]


def test_hook_cross_context_copy_keeps_unresolved_executor(tmp_path):
    a = {
        "type": "attachment",
        "uuid": "hook",
        "sessionId": "one",
        "attachment": {"type": "hook_result", "command": "echo ok"},
    }
    b = copy.deepcopy(a)
    b["sessionId"] = "two"
    _, data = built(tmp_path, {"a.jsonl": [a], "b.jsonl": [b]})
    assert rows(
        data, "select count(*),max(executor_id),max(session_id),max(attribution_status) from hooks"
    ) == [(1, None, None, "unresolved")]


def test_terminal_uuid_conflicting_provider_candidates_mark_count_uncertainty(tmp_path):
    _, data = built(
        tmp_path, {"a.jsonl": [assistant(mid="one")], "b.jsonl": [assistant(mid="two")]}
    )
    assert rows(data, "select count(*),sum(n_records),sum(output_tokens) from turns") == [
        (2, 2, None)
    ]
    assert rows(data, "select count(*) from fragments") == [(1,)]
    assert rows(data, "select count(*) from turns where count_uncertain") == [(2,)]
    assert rows(data, "select count(*) from turns where usage_available") == [(0,)]


def test_latest_repeated_title_and_utc_metadata_order(tmp_path):
    meta = [
        {"type": "ai-title", "sessionId": "s", "aiTitle": title, "timestamp": ts}
        for title, ts in [
            ("A", "2026-01-01T00:00:00Z"),
            ("B", "2026-01-01T00:00:00.100Z"),
            ("A", "2026-01-01T01:00:00.200+01:00"),
        ]
    ]
    _, data = built(tmp_path, {"a.jsonl": [assistant(), *meta]})
    assert rows(data, "select ai_title from sessions") == [("A",)]


def test_unresolved_context_bucket_keeps_valid_call_count(tmp_path):
    a = assistant(
        sid="one", content=[{"type": "tool_use", "id": "call", "name": "Bash", "input": {}}]
    )
    b = copy.deepcopy(a)
    b["sessionId"] = "two"
    _, data = built(tmp_path, {"a.jsonl": [a], "b.jsonl": [b]})
    assert rows(
        data,
        "select session_id,n_turns,n_tool_calls,n_main_turns,n_unattributed_turns from sessions",
    ) == [(None, 1, 1, 0, 1)]


def test_terminal_missing_usage_and_partial_vectors_have_explicit_denominators(tmp_path):
    absent = assistant("absent", "absent")
    absent["message"].pop("usage")
    partial = assistant("partial", "partial")
    partial["message"]["usage"] = {"output_tokens": 4}
    _, data = built(tmp_path, {"a.jsonl": [absent, partial]})
    assert rows(
        data,
        "select message_id,usage_available,input_tokens,output_tokens from turns order by message_id",
    ) == [("absent", False, None, None), ("partial", True, None, 4)]
    expected = {
        "turn_usage": (1, 1),
        "turn_input_tokens": (0, 2),
        "turn_output_tokens": (1, 1),
        "turn_cache_read": (0, 2),
        "turn_cache_create": (0, 2),
        "turn_cache_create_5m": (0, 2),
        "turn_cache_create_1h": (0, 2),
        "turn_thinking_tokens": (0, 2),
    }
    actual = {
        metric: (included, excluded)
        for metric, included, excluded in rows(data, "select * from metric_coverage")
    }
    assert all(actual[metric] == count for metric, count in expected.items())


def test_outer_behavioral_flag_population_cannot_upgrade_or_hide_conflict(tmp_path):
    normal = assistant()
    error = copy.deepcopy(normal)
    error["isApiErrorMessage"] = True
    error["apiErrorStatus"] = 429
    first = assistant("request", "request")
    first["requestId"] = 1
    second = copy.deepcopy(first)
    second["requestId"] = 2
    call = assistant(
        "call", "call", content=[{"type": "tool_use", "id": "tool", "name": "Bash", "input": {}}]
    )
    result = {
        "type": "user",
        "sessionId": "s",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "tool", "content": "ok"}]},
    }
    denied = copy.deepcopy(result)
    denied["toolDenialKind"] = "explicit"
    _, data = built(
        tmp_path, {"a.jsonl": [normal, first, call, result], "b.jsonl": [error, second, denied]}
    )
    assert rows(
        data,
        "select usage_available,final_usage_conflict from turns where message_id in ('m','request') order by message_id",
    ) == [(False, True), (False, True)]
    assert rows(data, "select result_valid,result_conflict from tool_calls") == [(False, True)]


def test_empty_provider_identity_api_records_do_not_merge(tmp_path):
    a = assistant("first", "")
    a["isApiErrorMessage"] = True
    b = assistant("second", "")
    b["isApiErrorMessage"] = True
    _, data = built(tmp_path, {"a.jsonl": [a, b]})
    assert rows(data, "select count(*),count(message_id),sum(n_records) from turns") == [(2, 0, 2)]
