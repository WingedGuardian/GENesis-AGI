"""Offline admission, actual store effects, and failure-boundary regressions."""

import asyncio
import copy
import hashlib
import tracemalloc
from dataclasses import replace

import pytest

from genesis.eval.qualification import contracts, corpus, preflight, storage
from genesis.eval.qualification.evidence import Incomplete, digest
from genesis.learning.procedural import embedding, extractor, operations, scoping, validation_gate
from tests.test_eval.qualification_fixtures import novelty_case
from tests.test_eval.qualification_preflight_fixtures import admitted, params


async def replay(tmp_path, case=None):
    case = case or novelty_case("probe", None)
    async with contracts.Sandbox(tmp_path) as sandbox:
        probe = contracts.Probe()
        await contracts.novelty(case, probe, sandbox)
        mapping = contracts.candidate_mapping(case, probe.calls[0][0], probe.candidate_ids)
        return await storage.replay(case, probe.calls[0][0], mapping,
                                    '{"redundant_with":null}', sandbox, "synthetic", "model")


@pytest.mark.parametrize("field", ["principle", "steps", "version", "confidence", "source",
                                 "success_count", "deprecated", "quarantined", "principle_embedding"])
async def test_mutating_any_original_column_is_detected(tmp_path, monkeypatch, field):
    original = operations.store_procedure_checked

    async def mutate(db, **kwargs):
        result = await original(db, **kwargs)
        # The instrument simulates a regression in the real store's side effects.
        await db.execute(f"UPDATE procedural_memory SET {field}=? WHERE id=?",
                         (b"changed" if field == "principle_embedding" else "changed", "candidate-a"))
        await db.commit()
        return result

    monkeypatch.setattr(operations, "store_procedure_checked", mutate)
    with pytest.raises(Incomplete, match="original candidate row"):
        await replay(tmp_path)


@pytest.mark.parametrize("path", ["judge", "extractor"])
@pytest.mark.parametrize("field", [
    "person_id", "scenario", "tools_used", "context_tags", "failure_count", "failure_modes",
    "last_used", "last_validated", "deprecated_reason", "superseded_by", "invocation_count",
    "attempted_workarounds", "version", "tool_trigger", "promotion_history", "surfaced_count",
    "extraction_context", "first_mover",
])
async def test_every_new_row_semantic_column_is_verified(tmp_path, monkeypatch, path, field):
    create = contracts.procedural.create

    async def corrupt(db, **kwargs):
        result = await create(db, **kwargs)
        tier = "DORMANT" if path == "judge" else "LIBRARY"
        if kwargs["id"] not in {"candidate-a", "candidate-b"} and kwargs.get("activation_tier") == tier:
            value = 42 if field in {"failure_count", "invocation_count", "version", "surfaced_count", "first_mover"} else "corrupted"
            await db.execute(f"UPDATE procedural_memory SET {field}=? WHERE id=?", (value, kwargs["id"]))
            await db.commit()
        return result

    monkeypatch.setattr(contracts.procedural, "create", corrupt)
    with pytest.raises(Incomplete, match="different procedure semantics"):
        await replay(tmp_path)


async def test_judge_input_metadata_and_extractor_fixture_metadata_stay_distinct(tmp_path):
    case = novelty_case("metadata", None)
    case["new"].update(tools_used="SyntheticTool", context_tags="SyntheticTag",
                       tool_trigger=["SyntheticTrigger"], scenario="Synthetic scenario")
    assert all(r["stored"] for r in (await replay(tmp_path, case)).values())


@pytest.mark.parametrize("flag", ["deprecated", "quarantined"])
async def test_excluded_original_rows_are_preserved(tmp_path, flag):
    case = novelty_case("excluded", None)
    clone = copy.deepcopy(case["existing"][0])
    clone.update(id="excluded", **{flag: True})
    case["existing"].append(clone)
    result = await replay(tmp_path, case)
    assert all(r["stored"] and r["original_candidates_preserved"] for r in result.values())


@pytest.mark.parametrize("state", ["directive", "unsuccessful", "malformed", "exception"])
async def test_actual_scoping_parser_and_fail_open_paths(tmp_path, monkeypatch, state):
    original = storage.Replay.route_call

    async def answer(router, call_site_id, messages, **kwargs):
        result = await original(router, call_site_id, messages, **kwargs)
        if call_site_id == scoping._CALL_SITE and "Procedure under review:" in messages[0]["content"]:
            if state == "exception":
                raise RuntimeError("synthetic classifier failure")
            result = replace(result, success=state != "unsuccessful",
                             content='{"procedure_type":"behavioral_directive"}'
                             if state == "directive" else "unparseable synthetic response")
        return result

    monkeypatch.setattr(storage.Replay, "route_call", answer)
    result = await replay(tmp_path)
    assert all(r["stored"] is (state != "directive") for r in result.values())
    assert all(r["novelty_replayed"] is (state != "directive") for r in result.values())


@pytest.mark.parametrize("mode", [1, 2, 3, 4])
async def test_cancellation_closes_database_and_restores_globals(tmp_path, monkeypatch, mode):
    original = storage.Replay.route_call
    calls, handles = 0, []
    database = contracts.Sandbox.database
    allowlist = dict(extractor._NOVELTY_VALIDATED_MODELS)
    cooldown = dict(embedding._fail_open_timestamps)
    validator, classifier = validation_gate.validate_extraction, scoping.is_behavioral_directive

    async def opening(sandbox, case):
        db = await database(sandbox, case)
        handles.append(db)
        return db

    async def cancelled(router, call_site_id, messages, **kwargs):
        nonlocal calls
        if call_site_id == extractor._NOVELTY_CALL_SITE:
            calls += 1
            if calls == mode:
                embedding._fail_open_timestamps["synthetic-cancel"] = 1
                raise asyncio.CancelledError
        return await original(router, call_site_id, messages, **kwargs)

    monkeypatch.setattr(contracts.Sandbox, "database", opening)
    monkeypatch.setattr(storage.Replay, "route_call", cancelled)
    with pytest.raises(asyncio.CancelledError):
        await replay(tmp_path)
    assert allowlist == extractor._NOVELTY_VALIDATED_MODELS
    assert embedding._fail_open_timestamps == cooldown
    assert validation_gate.validate_extraction is validator
    assert scoping.is_behavioral_directive is classifier
    for db in handles:
        with pytest.raises(ValueError):
            await db.execute("SELECT 1")
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(storage.Replay, "route_call", original)
    assert len(await replay(tmp_path)) == 4


@pytest.mark.parametrize("change", ["prefix", "candidate_order", "mapping"])
async def test_saved_answer_cannot_bind_another_rendered_question(tmp_path, change):
    case = novelty_case("question", None)
    async with contracts.Sandbox(tmp_path) as sandbox:
        probe = contracts.Probe()
        await contracts.novelty(case, probe, sandbox)
        messages = copy.deepcopy(probe.calls[0][0])
        mapping = contracts.candidate_mapping(case, messages, probe.candidate_ids)
        if change == "prefix":
            messages[0]["content"] = "Changed question\n" + messages[0]["content"]
        elif change == "candidate_order":
            messages[0]["content"] = messages[0]["content"].replace("task-b-question", "other-task")
        else:
            mapping.reverse()
        with pytest.raises(Incomplete):
            await storage.replay(case, messages, mapping, '{"redundant_with":null}', sandbox,
                                 "synthetic", "model")


@pytest.mark.parametrize("variant", ["ids", "candidate_tail", "new_tail", "metadata"])
async def test_actual_duplicate_question_classes(tmp_path, variant):
    first = novelty_case("original", None)
    second = copy.deepcopy(first)
    second["id"] = "different-case"
    if variant == "ids":
        second["existing"][0]["id"] = "different-row"
    elif variant == "metadata":
        second["new"]["unused"] = "different"
    else:
        a = first["new"] if variant == "new_tail" else first["existing"][0]
        b = second["new"] if variant == "new_tail" else second["existing"][0]
        a["steps"] = ["x" * 160 + "a"]
        b["steps"] = ["x" * 160 + "b"]
    with pytest.raises(Incomplete, match="duplicate rendered"):
        await preflight.prepare({corpus.NOVELTY: [first, second]}, tmp_path)


@pytest.mark.parametrize("change", ["missing_contract", "class_floor", "policy", "approval",
                                   "actor", "version", "source", "source_path", "params"])
async def test_complete_admission_refuses_before_rendering_or_credentials(tmp_path, monkeypatch, change):
    cases, options = admitted(tmp_path)
    row = cases[corpus.NOVELTY][0]
    if change == "missing_contract":
        cases.pop(corpus.RELEVANCE)
    elif change == "class_floor":
        cases[corpus.NOVELTY].pop()
    elif change == "policy":
        options["current_policy"] = {**options["current_policy"], "guidance": "Changed"}
    elif change == "approval":
        options["reference_approval"]["approved"] = False
    elif change == "actor":
        row["reference_review"].pop("label_source")
    elif change == "version":
        row["reference_provenance"]["rubric_version"] = "old"
    elif change == "source":
        from pathlib import Path
        Path(next(iter(options["source_files"]))).write_text("changed")
    elif change == "source_path":
        row["source"]["snapshot"] = "unbound-path"
    else:
        options["candidates"]["openrouter-mimo"]["judge"]["max_tokens"] = 0
    if change in ("missing_contract", "class_floor", "source_path"):
        options["reference_approval"]["corpus_hash"] = digest(cases)
        from genesis.eval.qualification import references
        for name, rows in cases.items():
            for case in rows:
                case["reference_review"]["case_hash"] = references.case_hash(name, case)
        options["reference_approval"]["corpus_hash"] = digest(cases)

    async def never(*_args, **_kwargs):
        raise AssertionError("admission failure reached rendering")

    monkeypatch.setattr(preflight, "prepare", never)
    with pytest.raises(Incomplete):
        await preflight.check(cases, tmp_path, **options)


@pytest.mark.parametrize("value", [True, 0.0, 1, None])
def test_chain_offset_requires_exact_supported_integer(value):
    plan = {corpus.RELEVANCE: {"case": {"call_parameters": {"chain_offset": value}}}}
    with pytest.raises(Incomplete, match="call-site parameter"):
        preflight.parameters("openrouter-mimo", params(), plan)


def test_source_hashing_streams_large_snapshot_with_bounded_memory(tmp_path, monkeypatch):
    from pathlib import Path

    path = tmp_path / "source.sqlite"
    with path.open("wb") as file:
        file.truncate(64 * 1024 * 1024)
    expected = hashlib.sha256()
    for _ in range(64 * 256):
        expected.update(bytes(4096))

    def no_whole_file(_path):
        raise AssertionError("source hash loaded a whole file into memory")

    monkeypatch.setattr(Path, "read_bytes", no_whole_file)
    tracemalloc.start()
    try:
        actual = preflight.file_hash(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert actual == expected.hexdigest()
    assert peak < 1024 * 1024


@pytest.mark.parametrize("missing", [False, True])
def test_composite_sources_check_every_nested_arm_and_git_baseline(tmp_path, missing):
    path = tmp_path / "export"
    path.write_text("historical bytes")
    sha = preflight.file_hash(path)
    source = {"query_source": {"path": str(path), "sha256": sha}, "baselines": [
        {"path": "historical/repo/path", "commit": "historical-commit",
         "sha256": "0" * 64 if missing else sha}
    ]}
    cases = {"test": [{"source": source}]}
    if missing:
        with pytest.raises(Incomplete, match="source hash"):
            preflight.source_evidence(cases, {str(path): sha})
    else:
        assert preflight.source_evidence(cases, {str(path): sha}) == {str(path): sha}
