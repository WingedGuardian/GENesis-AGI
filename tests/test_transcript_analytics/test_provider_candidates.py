"""Contradictions stay attached to every implicated provider candidate."""

import json

import pytest

from genesis.transcript_analytics import derive, query, store


def row(event, provider, *, terminal=False, context="parent"):
    return {
        "type": "assistant",
        "sessionId": context,
        "uuid": event,
        "message": {
            "id": provider,
            "model": "model",
            "stop_reason": "end_turn" if terminal else None,
            "content": [{"type": "text", "text": event}],
            "usage": {"output_tokens": 7},
        },
    }


@pytest.mark.parametrize("other_provider", [None, "other"])
@pytest.mark.parametrize("reverse", [False, True])
def test_provider_siblings_keep_contrary_event_evidence(tmp_path, other_provider, reverse):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    files = [
        ("a.jsonl", [row("early", "provider"), row("final", "provider", terminal=True)]),
        ("b.jsonl", [row("early", other_provider)]),
    ]
    if reverse:
        files.reverse()
    for name, items in files:
        (projects / name).write_text("".join(json.dumps(item) + "\n" for item in items))
    assert store.ingest(projects, data, lock_path=tmp_path / "writer.lock")["failed"] == 0
    columns, values = query.run_query(data, "SELECT * FROM turns ORDER BY turn_key", live=True)
    turns = [dict(zip(columns, value, strict=True)) for value in values]
    candidates = {turn["message_id"]: turn for turn in turns}
    assert len(turns) == (1 if other_provider is None else 2)
    turn = candidates["provider"]
    assert turn["usage_available"] is False
    assert turn["output_tokens"] is None
    assert {(ref["source_file"], ref["line_no"]) for ref in turn["source_references"]} == {
        ("a.jsonl", 1),
        ("a.jsonl", 2),
        ("b.jsonl", 1),
    }
    assert turn["physical_observation_count"] == 3
    if other_provider is not None:
        for candidate in turns:
            assert candidate["identity_conflict"] is True
            assert candidate["count_uncertain"] is True
            assert candidate["executor_id"] is None
            assert candidate["attribution_status"] == "conflicting"
            assert candidate["usage_available"] is False
        assert {
            (ref["source_file"], ref["line_no"]) for ref in candidates["other"]["source_references"]
        } == {("a.jsonl", 1), ("b.jsonl", 1)}


@pytest.mark.parametrize("same_source", [False, True])
@pytest.mark.parametrize("unknown_copy", [False, True])
def test_every_present_candidate_survives_without_inventing_unknown_turn(
    tmp_path, same_source, unknown_copy
):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    first = [row("early", "provider"), row("final", "provider", terminal=True)]
    other = row("early", "other")
    files = {"a.jsonl": first}
    if same_source:
        first.append(other)
    else:
        files["b.jsonl"] = [other]
    if unknown_copy:
        files["c.jsonl"] = [row("early", None)]
    for name, items in files.items():
        (projects / name).write_text("".join(json.dumps(item) + "\n" for item in items))
    assert store.ingest(projects, data, lock_path=tmp_path / "writer.lock")["failed"] == 0
    columns, values = query.run_query(data, "SELECT * FROM turns ORDER BY turn_key", live=True)
    turns = [dict(zip(columns, value, strict=True)) for value in values]
    assert len(turns) == 2
    assert {turn["message_id"] for turn in turns} == {"provider", "other"}
    common = {("a.jsonl", 1), ("a.jsonl", 3) if same_source else ("b.jsonl", 1)}
    if unknown_copy:
        common.add(("c.jsonl", 1))
    for turn in turns:
        assert turn["count_uncertain"] and turn["identity_conflict"]
        assert turn["executor_id"] is None and not turn["usage_available"]
        expected = common | ({("a.jsonl", 2)} if turn["message_id"] == "provider" else set())
        assert {
            (ref["source_file"], ref["line_no"]) for ref in turn["source_references"]
        } == expected
        assert turn["physical_observation_count"] == len(expected)


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("scenario", ["empty", "known", "uncertain"])
def test_query_and_snapshot_provenance_disclose_candidate_denominators(tmp_path, live, scenario):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    if scenario != "empty":
        (projects / "a.jsonl").write_text(
            "".join(
                json.dumps(item) + "\n"
                for item in [row("early", "provider"), row("final", "provider", terminal=True)]
            )
        )
        (projects / "b.jsonl").write_text(
            json.dumps(row("early", "other" if scenario == "uncertain" else "provider")) + "\n"
        )
    assert store.ingest(projects, data, lock_path=tmp_path / "writer.lock")["failed"] == 0
    derive.build(data, lock_path=tmp_path / "writer.lock")
    manifest = tmp_path / "query-manifest.json"
    query.run_query(data, "SELECT * FROM sessions", live=live, manifest_path=manifest)
    candidates = 0 if scenario == "empty" else 2 if scenario == "uncertain" else 1
    uncertain = 2 if scenario == "uncertain" else 0
    expected = {
        "candidate_rows": candidates,
        "uncertain_candidate_rows": uncertain,
        "unambiguous_candidate_rows": candidates - uncertain,
    }
    published = query.snapshot_manifest(data)
    saved = json.loads(manifest.read_text())
    for summary in [published, saved]:
        assert summary["denominators"]["turns"] == candidates
        assert summary["denominator_basis"]["turns"] == "candidate_turn_rows"
        assert summary["turn_identity_coverage"] == expected
    if not live:
        assert saved["snapshot"]["turn_identity_coverage"] == expected


@pytest.mark.parametrize("flag", ["api_error", "scrub_failed"])
@pytest.mark.parametrize("unknown_copy", [False, True])
@pytest.mark.parametrize("flagged_provider", ["provider", "other"])
def test_every_provider_candidate_keeps_physical_failure_flags(
    tmp_path, flag, unknown_copy, flagged_provider
):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    first, second = row("early", "provider"), row("early", "other")
    flagged = first if flagged_provider == "provider" else second
    if flag == "api_error":
        flagged.update(isApiErrorMessage=True, apiErrorStatus=429)
    else:
        flagged["message"]["model"] = '{"password":'
    files = {"a.jsonl": [first, row("final", "provider", terminal=True)], "b.jsonl": [second]}
    if unknown_copy:
        files["c.jsonl"] = [row("early", None)]
    for name, items in files.items():
        (projects / name).write_text("".join(json.dumps(item) + "\n" for item in items))
    assert store.ingest(projects, data, lock_path=tmp_path / "writer.lock")["failed"] == 0
    _columns, turns = query.run_query(
        data,
        "SELECT is_api_error, scrub_failed, count_uncertain, usage_available FROM turns",
        live=True,
    )
    assert len(turns) == 2
    assert turns == [(flag == "api_error", flag == "scrub_failed", True, False)] * 2
