"""Source evidence must not choose an owner from unrelated reused tool IDs."""

import json

import pytest

from genesis.transcript_analytics import evidence, store


@pytest.mark.parametrize("tool_id", ["reused", "reused-" + "x" * 2048])
def test_unrelated_contexts_reusing_tool_id_report_ambiguity(tmp_path, tool_id):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    for number in (1, 2):
        record = {
            "type": "assistant", "uuid": f"event-{number}",
            "sessionId": f"context-{number}",
            "message": {"id": f"message-{number}", "model": "fixture", "content": [
                {"type": "tool_use", "id": tool_id, "name": "Bash",
                 "input": {"command": f"echo fixture-{number}"}},
            ]},
        }
        (projects / f"context-{number}.jsonl").write_text(json.dumps(record) + "\n")
    assert store.ingest(projects, data)["rebuilt"] == 2
    result = evidence.evidence(data, projects, tool_id, surrounding=0, budget=1024)
    assert result.get("candidate_count") == 2
    assert "ambiguous" in result["unavailable"]
    assert "attribution" not in result
    assert not result.get("references")
    assert len(json.dumps(result, ensure_ascii=False).encode()) <= 1024


@pytest.mark.parametrize("context_size", [288, 292, 296])
def test_final_target_availability_stays_within_byte_budget(tmp_path, context_size):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    tool_id = "identity-" + "t" * 50
    context = "s" * context_size
    call = {
        "type": "assistant", "sessionId": context, "uuid": "call",
        "message": {"id": "message", "model": "fixture", "content": [
            {"type": "tool_use", "id": tool_id, "name": "Bash",
             "input": {"command": "echo safe" + "x" * 5000}},
        ]},
    }
    result = {
        "type": "user", "sessionId": context, "uuid": "result",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": tool_id,
             "content": "done" + "x" * 5000},
        ]},
    }
    for name, record in (("call.jsonl", call), ("result.jsonl", result)):
        (projects / name).write_text(json.dumps(record) + "\n")
    assert store.ingest(projects, data)["failed"] == 0
    out = evidence.evidence(data, projects, tool_id, surrounding=0, budget=2048)
    assert out["reference_count"] == 2
    assert len(json.dumps(out, ensure_ascii=False).encode()) <= 2048
    for ref in out["references"]:
        if "records" in ref:
            assert ref["target_available"] == evidence._target_available(
                ref["records"], ref["line"], tool_id,
            )
