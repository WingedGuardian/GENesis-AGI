import io
import json

import pytest

from genesis.transcript_analytics import evidence
from genesis.transcript_analytics.extract import _hash


def _record(blocks, **fields):
    return (json.dumps({"message": {"content": blocks}, **fields}) + "\n").encode()


def _call(value="original"):
    return {"type": "tool_use", "id": "example", "name": "Bash", "input": {"command": value}}


def test_same_id_changed_payload_is_not_original_evidence(tmp_path):
    (tmp_path / "call.jsonl").write_bytes(_record([_call("changed")]))
    out = evidence.read_reference(
        tmp_path,
        "call.jsonl",
        1,
        expected_id="example",
        expected_hash=_hash(_call()),
        expected_kind="call",
        archive_dir=tmp_path / "absent",
    )
    assert "unavailable" in out
    assert (tmp_path / "call.jsonl").is_file()
    assert out["unavailable"] == "original target and local encrypted archive unavailable"
    assert "records" not in out


def test_duplicate_record_with_matching_id_is_not_projected():
    raw = b'{"message":{"content":[{"type":"tool_use","id":"example"}]},"message":{"content":[{"type":"tool_use","id":"example"}]}}'
    out = evidence._window(io.BytesIO(raw), 1, 0, 4096, expected_id="example")
    assert not out["target_available"]
    assert not out["records"]


def test_evidence_decode_budget_withholding_is_separate_from_output_clipping():
    block = _call('"x" ' * 65)
    out = evidence._window(
        io.BytesIO(_record([block])),
        1,
        0,
        65536,
        expected_id="example",
        expected_hash=_hash(block),
        expected_kind="call",
    )
    assert out["records"][0]["scrub_failed"]
    assert not out["target_available"]
    assert out["records"][0]["text"] == "[scrub failed: evidence withheld]"


@pytest.mark.parametrize("denial", [None, "tool_denied", 10])
@pytest.mark.parametrize("multiple", [False, True])
def test_result_hash_matches_extractor_normalization(denial, multiple):
    block = {"type": "tool_result", "tool_use_id": "example", "content": "done"}
    blocks = [block] + ([{"type": "tool_result", "tool_use_id": "other"}] if multiple else [])
    tur = {"agentId": "child"}
    raw = _record(blocks, toolUseResult=tur, toolDenialKind=denial)
    digest = _hash(
        {
            "block": block,
            "toolUseResult": None if multiple else tur,
            "denial_kind": denial if isinstance(denial, str) else None,
        }
    )
    out = evidence._window(
        io.BytesIO(raw),
        1,
        0,
        4096,
        expected_id="example",
        expected_hash=digest,
        expected_kind="result",
    )
    assert out["target_available"]


def test_distinct_same_id_blocks_require_exact_observation_hash():
    original, changed = _call(), _call("changed")
    raw = _record([original, changed])
    assert not evidence._window(io.BytesIO(raw), 1, 0, 4096, expected_id="example")[
        "target_available"
    ]
    out = evidence._window(
        io.BytesIO(raw),
        1,
        0,
        4096,
        expected_id="example",
        expected_hash=_hash(original),
        expected_kind="call",
    )
    assert out["target_available"]
    assert "changed" not in out["records"][0]["text"]


def test_evidence_enumerates_physical_conflicts_without_assigning_owner(tmp_path, monkeypatch):
    rows = []
    for source, block in [("a.jsonl", _call()), ("b.jsonl", _call("changed"))]:
        (tmp_path / source).write_bytes(_record([block]))
        rows.append(
            (
                source,
                1,
                None,
                _hash(block),
                None,
                None,
                "conflict",
                "conflicting observations",
                None,
                "unknown",
                ["context"],
                True,
                False,
                False,
                True,
                1,  # One logical candidate, several physical observations.
            )
        )
    monkeypatch.setattr(evidence, "run_query", lambda *args, **kwargs: ([], rows + [rows[0]]))
    out = evidence.evidence(tmp_path, tmp_path, "example", surrounding=0)
    assert out["reference_count"] == 2
    assert [ref["source"] for ref in out["references"] if "source" in ref] == ["a.jsonl", "b.jsonl"]
    assert out["attribution"]["executor_id"] is None
    assert out["attribution"]["content_conflict"]
    assert all(ref["target_available"] for ref in out["references"] if "source" in ref)


def test_scrub_failed_placeholder_never_claims_target_available(monkeypatch):
    monkeypatch.setattr(evidence.scrub, "scrub_text", lambda text: (None, True))
    out = evidence._window(io.BytesIO(b"sensitive"), 1, 0, 4096)
    assert not out["target_available"]
    assert out["records"][0]["scrub_failed"]


def test_reference_population_shares_hydration_budget(tmp_path, monkeypatch):
    rows = [
        (
            f"copy-{index}.jsonl",
            1,
            None,
            _hash(_call()),
            None,
            None,
            "unknown",
            "missing ownership evidence",
            None,
            "unknown",
            [],
            False,
            False,
            True,
            True,
            1,  # Physical copies do not increase derived-candidate count.
        )
        for index in range(12)
    ]
    budgets = []

    def read(*args, **kwargs):
        budgets.append(kwargs["budget"])
        return {"records": [], "target_available": False}

    monkeypatch.setattr(evidence, "run_query", lambda *args, **kwargs: ([], rows))
    monkeypatch.setattr(evidence, "read_reference", read)
    out = evidence.evidence(tmp_path, tmp_path, "example", budget=4096)
    assert out["reference_count"] == 12
    assert len(budgets) == 12
    assert sum(budgets) <= 4096
    assert len(json.dumps(out).encode()) <= 4096


def test_derived_snapshot_evidence_uses_all_raw_references(tmp_path, monkeypatch):
    from genesis.transcript_analytics import derive, store

    monkeypatch.setenv("TA_DUCKDB_TMP", str(tmp_path / "duckdb"))
    projects, data = tmp_path / "projects", tmp_path / "data"
    session = "11111111-2222-3333-4444-555555555555"
    call = {
        "type": "assistant",
        "uuid": "call-copy",
        "sessionId": session,
        "timestamp": "2026-10-01T00:00:00Z",
        "message": {"id": "message-copy", "model": "synthetic", "content": [_call()]},
    }
    result = {
        "type": "user",
        "uuid": "result-copy",
        "sessionId": session,
        "timestamp": "2026-10-01T00:00:01Z",
        "message": {
            "content": [{"type": "tool_result", "tool_use_id": "example", "content": "done"}]
        },
    }
    sources = []
    for project in ("p", "q"):
        source = projects / project / (session + ".jsonl")
        source.parent.mkdir(parents=True)
        source.write_text(json.dumps(call) + "\n" + json.dumps(result) + "\n")
        sources.append(source)
    assert store.ingest(projects, data)["rebuilt"] == 2
    derive.build(data)
    assert (data / "derived/current").is_dir()
    original_read = evidence.read_reference

    def isolated_read(*args, **kwargs):
        return original_read(*args, archive_dir=tmp_path / "archives", **kwargs)

    monkeypatch.setattr(evidence, "read_reference", isolated_read)
    before = evidence.evidence(data, projects, "example", surrounding=0)
    assert before["reference_count"] == 4
    assert all(ref["target_available"] for ref in before["references"])
    call["message"]["content"][0]["input"]["command"] = "changed same-ID command"
    sources[0].write_text(json.dumps(call) + "\n" + json.dumps(result) + "\n")
    after = evidence.evidence(data, projects, "example", surrounding=0)
    assert after["reference_count"] == 4
    unavailable = [ref for ref in after["references"] if "unavailable" in ref]
    assert len(unavailable) == 1 and unavailable[0]["kind"] == "call"
    assert "changed same-ID command" not in json.dumps(after)
