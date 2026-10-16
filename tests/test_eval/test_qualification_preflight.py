"""Offline admission, actual store effects, and failure-boundary regressions."""

import asyncio
import copy
import functools
import hashlib
import json
import tracemalloc
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.eval.qualification import contracts, corpus, preflight, storage
from genesis.eval.qualification.evidence import Incomplete, digest
from genesis.learning.procedural import embedding, extractor, operations, scoping, validation_gate
from tests.test_eval.qualification_fixtures import novelty_case
from tests.test_eval.qualification_preflight_fixtures import admitted, params


async def replay(tmp_path, case=None, content='{"redundant_with":null}'):
    case = case or novelty_case("probe", None)
    async with contracts.Sandbox(tmp_path) as sandbox:
        probe = contracts.Probe()
        rendered = await contracts.novelty(case, probe, sandbox)
        mapping = contracts.candidate_mapping(case, probe.calls[0][0], rendered["candidate_ids"])
        return await storage.replay(case, probe.calls[0][0], mapping,
                                    content, sandbox, "synthetic", "model")


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
    "extraction_context", "first_mover", "trigger_spec", "lesson",
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


@pytest.mark.parametrize("trigger,expected,spec", [
    (["SyntheticTrigger"], None, None),
    (["Bash"], ["Bash"], None),
    (["Bash (running tests)"], ["Bash"], None),
    (["Read", "Read", "SyntheticTrigger"], ["Read"], None),
    (["ssh"], ["Bash"], {"patterns": [], "commands": ["ssh"], "repeat": "every"}),
    (["ssh", "Bash"], ["Bash"], None),
    (["Read", "ssh"], ["Read", "ssh"], None),
    (["ssh", "scp", "pytest", "ruff", "gitleaks", "pgrep", "incus", "gh", "git push"],
     ["ssh", "scp", "pytest", "ruff", "gitleaks", "pgrep", "incus", "gh", "git push"], None),
])
async def test_judge_input_metadata_and_extractor_fixture_metadata_stay_distinct(
    tmp_path, monkeypatch, trigger, expected, spec,
):
    case = novelty_case("metadata", None)
    case["new"].update(tools_used="SyntheticTool", context_tags="SyntheticTag",
                       tool_trigger=trigger, scenario="Synthetic scenario")
    original, inserted = storage.rows, []

    async def observe(db):
        result = await original(db)
        inserted.extend(row for key, row in result.items()
                        if key not in {"candidate-a", "candidate-b"})
        return result

    monkeypatch.setattr(storage, "rows", observe)
    assert all(r["stored"] for r in (await replay(tmp_path, case)).values())
    assert len(inserted) == 8  # writer plus independent committed reader in each mode
    for index, row in enumerate(inserted):
        judge_path = index // 2 % 2 == 0
        assert json.loads(row["tool_trigger"] or "null") == (expected if judge_path else None)
        assert json.loads(row["trigger_spec"] or "null") == (spec if judge_path else None)
        assert row["lesson"] is None


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

    @asynccontextmanager
    async def opening(sandbox, case):
        async with database(sandbox, case) as db:
            handles.append(db)
            yield db

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
        rendered = await contracts.novelty(case, probe, sandbox)
        messages = copy.deepcopy(probe.calls[0][0])
        mapping = contracts.candidate_mapping(case, messages, rendered["candidate_ids"])
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
        {"path": "historical/repo/path", "commit": "a" * 40,
         "sha256": "0" * 64 if missing else sha}
    ]}
    cases = {"test": [{"source": source}]}
    if missing:
        with pytest.raises(Incomplete, match="source hash"):
            preflight.source_evidence(cases, {str(path): sha})
    else:
        assert preflight.source_evidence(cases, {str(path): sha}) == {str(path): sha}


@pytest.mark.parametrize("commit", [None, "", " ", False, {}, [], 42, "not-a-commit", "a" * 39])
@pytest.mark.parametrize("hash_key", ["sha256", "before_sha256", "after_sha256"])
def test_malformed_git_source_cannot_relax_path_binding(tmp_path, commit, hash_key):
    path = tmp_path / "export"
    path.write_text("historical bytes")
    sha = preflight.file_hash(path)
    case = {"source": {"path": "wrong", "commit": commit, hash_key: sha}}
    with pytest.raises(Incomplete):
        preflight.source_evidence({"test": [case]}, {str(path): sha})


@pytest.mark.parametrize("path", [None, "", ".", "/absolute", "../outside", "nul\x00path"])
def test_historical_source_requires_repository_path(tmp_path, path):
    export = tmp_path / "export"
    export.write_text("historical bytes")
    sha = preflight.file_hash(export)
    case = {"source": {"path": path, "commit": "a" * 40, "before_sha256": sha}}
    with pytest.raises(Incomplete):
        preflight.source_evidence({"test": [case]}, {str(export): sha})


@pytest.mark.parametrize("length", [40, 64])
@pytest.mark.parametrize("paired", [False, True])
def test_valid_git_exports_retain_both_byte_arms(tmp_path, length, paired):
    exports = [tmp_path / "before", tmp_path / "after"]
    for index, path in enumerate(exports):
        path.write_text(f"historical bytes {index}")
    sources = {str(path): preflight.file_hash(path) for path in exports}
    node = {"commit": "a" * length, "path": "historical/repo/path"}
    if paired:
        node.update(parent="b" * length, before_sha256=sources[str(exports[0])],
                    after_sha256=sources[str(exports[1])])
    else:
        node["sha256"] = sources[str(exports[0])]
    assert preflight.source_evidence({"test": [{"source": node}]}, sources) == sources


@pytest.mark.parametrize("key", ["sha256", "snapshot_sha256", "before_sha256", "after_sha256"])
def test_missing_commit_retains_ordinary_path_binding(tmp_path, key):
    export = tmp_path / "export"
    export.write_text("bytes")
    sha = preflight.file_hash(export)
    path_key = "path" if key == "sha256" else key.removesuffix("_sha256")
    with pytest.raises(Incomplete, match="source path"):
        preflight.source_evidence({"test": [{"source": {path_key: "wrong", key: sha}}]},
                                  {str(export): sha})


@pytest.mark.parametrize("git_hashes", ["none", "single", "paired"])
@pytest.mark.parametrize("other", ["snapshot", "transcript", "before_snapshot"])
def test_git_exception_never_waives_other_source_paths(tmp_path, git_hashes, other):
    export = tmp_path / "export"
    export.write_text("historical bytes")
    sha = preflight.file_hash(export)
    node = {"commit": "a" * 40, "path": "historical/path",
            other: "wrong", other + "_sha256": sha}
    if git_hashes == "single":
        node["sha256"] = sha
    elif git_hashes == "paired":
        node.update(parent="b" * 40, before_sha256=sha, after_sha256=sha)
    with pytest.raises(Incomplete, match="source path"):
        preflight.source_evidence({"test": [{"source": node}]}, {str(export): sha})


@pytest.mark.parametrize("missing", ["before_sha256", "after_sha256", "parent"])
def test_paired_git_export_requires_both_arms_and_parent(tmp_path, missing):
    export = tmp_path / "export"
    export.write_text("historical bytes")
    sha = preflight.file_hash(export)
    node = {"commit": "a" * 40, "path": "historical/path", "parent": "b" * 40,
            "before_sha256": sha, "after_sha256": sha}
    node.pop(missing)
    with pytest.raises(Incomplete, match="historical"):
        preflight.source_evidence({"test": [{"source": node}]}, {str(export): sha})


@pytest.mark.parametrize("parent", [None, "", " ", False, {}, [], 42, "not-an-id", "a" * 39])
def test_paired_git_export_requires_full_parent_identifier(tmp_path, parent):
    export = tmp_path / "export"
    export.write_text("historical bytes")
    sha = preflight.file_hash(export)
    node = {"commit": "a" * 40, "path": "historical/path", "parent": parent,
            "before_sha256": sha, "after_sha256": sha}
    with pytest.raises(Incomplete, match="historical"):
        preflight.source_evidence({"test": [{"source": node}]}, {str(export): sha})


@pytest.mark.parametrize("mode", [1, 2, 3, 4])
@pytest.mark.parametrize("suppress", [False, True])
async def test_uncommitted_store_effects_are_refused(tmp_path, monkeypatch, mode, suppress):
    from genesis.learning.procedural import judge

    calls = 0

    def wrap(original):
        async def store(db, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == mode:
                monkeypatch.setattr(db, "commit", AsyncMock())
            result = await original(db, *args, **kwargs)
            if calls == mode and suppress:
                await db.execute("UPDATE procedural_memory SET success_count=success_count")
            return result
        return store

    monkeypatch.setattr(judge, "_store_judged_procedure", wrap(judge._store_judged_procedure))
    monkeypatch.setattr(extractor, "extract_procedure", wrap(extractor.extract_procedure))
    if suppress:
        monkeypatch.setitem(extractor._NOVELTY_VALIDATED_MODELS, "synthetic", "model")
    content = '{"redundant_with":1}' if suppress else '{"redundant_with":null}'
    with pytest.raises(Incomplete):
        await replay(tmp_path, content=content)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("field", ["tools_used", "context_tags"])
@pytest.mark.parametrize("value", [None, {}, {"bad": "shape"}, 0, False, [None], [["Bash"]], ["Bash", 1]])
async def test_invalid_metadata_refused_before_database(tmp_path, monkeypatch, field, value):
    case = novelty_case("bad-metadata", None)
    case["new"][field] = value

    def never(*args, **kwargs):
        raise AssertionError("invalid metadata reached database")

    monkeypatch.setattr(contracts.Sandbox, "database", never)
    with pytest.raises(Incomplete, match="metadata"):
        await preflight.prepare({corpus.NOVELTY: [case]}, tmp_path)


@pytest.mark.parametrize("value", ["", "Bash", [], ["Bash"], ["", "Bash"]])
@pytest.mark.parametrize("field", ["tools_used", "context_tags"])
async def test_supported_metadata_shapes_are_preserved(tmp_path, field, value):
    case = novelty_case("metadata-shape", None)
    case["new"][field] = value
    assert all(row["stored"] for row in (await replay(tmp_path, case)).values())


@pytest.mark.parametrize("module_name", ["_migrations", "_tables"])
async def test_schema_sources_bound_and_rechecked(tmp_path, monkeypatch, module_name):
    from genesis.db.schema import _migrations, _tables

    module = {"_migrations": _migrations, "_tables": _tables}[module_name]
    cases, options = admitted(tmp_path)
    original = preflight.file_hash
    changed = False

    def file_hash(path):
        return "0" * 64 if changed and str(path) == module.__file__ else original(path)

    async def prepare(*args, **kwargs):
        nonlocal changed
        changed = True
        return {}

    monkeypatch.setattr(preflight, "file_hash", file_hash)
    monkeypatch.setattr(preflight, "prepare", prepare)
    with pytest.raises(Incomplete, match="source or contract changed"):
        await preflight.check(cases, tmp_path, **options)


async def test_complete_check_binds_actual_normalizer_registry_inputs(tmp_path, monkeypatch):
    from genesis.cc import session_config, types
    from genesis.learning.procedural import tool_names

    attempted, read_text = set(), Path.read_text

    def observe(path, *args, **kwargs):
        attempted.add(path)
        return read_text(path, *args, **kwargs)

    with monkeypatch.context() as observer:
        observer.setattr(Path, "read_text", observe)
        tool_names._mcp_owners.__wrapped__()
    attempted.update(Path(m.__file__) for m in (session_config, types))
    cases, options = admitted(tmp_path)
    monkeypatch.setattr(preflight, "prepare", AsyncMock(return_value={}))
    result = await preflight.check(cases, tmp_path, **options)
    root = Path(preflight.__file__).resolve().parents[4]
    for path in attempted:
        assert result["production_sources"][str(path.relative_to(root))] == preflight.file_hash(path)


@pytest.fixture
def registry_files(monkeypatch):
    """Virtual registry additions exercise public admission without editing the checkout."""
    from genesis.learning.procedural import tool_names

    health = Path(tool_names.__file__).resolve().parents[2] / "mcp" / "health"
    files = {"_qualification_registry_fixture.py": ""}
    assert not (health / next(iter(files))).exists()
    glob, iterdir, read_text, file_hash = Path.glob, Path.iterdir, Path.read_text, preflight.file_hash

    def children():
        return {health / name.split("/", 1)[0] for name in files}

    def glob_sources(path, pattern, **kwargs):
        yield from glob(path, pattern, **kwargs)
        if path == health:
            yield from (child for child in children() if child.match(pattern))

    def iter_sources(path):
        yield from iterdir(path)
        if path == health:
            yield from children()

    def source_text(path, *args, **kwargs):
        if path.parent == health and path.name in files:
            return files[path.name]
        return read_text(path, *args, **kwargs)

    def source_hash(path):
        path = Path(path)
        if path.parent == health and path.name in files:
            return hashlib.sha256(files[path.name].encode()).hexdigest()
        return file_hash(path)

    monkeypatch.setattr(Path, "glob", glob_sources)
    monkeypatch.setattr(Path, "iterdir", iter_sources)
    monkeypatch.setattr(Path, "read_text", source_text)
    monkeypatch.setattr(preflight, "file_hash", source_hash)
    # Isolate the test cache without clearing the production cache.
    cache = functools.cache(tool_names._mcp_owners.__wrapped__)
    monkeypatch.setattr(tool_names, "_mcp_owners", cache)
    return files, cache


@pytest.mark.parametrize("change", ["add", "remove", "modify", "literal", "hidden"])
async def test_complete_check_rechecks_registry_population(tmp_path, monkeypatch, registry_files, change):
    files, _cache = registry_files
    cases, options = admitted(tmp_path)

    async def prepare(*args, **kwargs):
        if change == "remove":
            files.clear()
        elif change == "modify":
            files[next(iter(files))] = '"changed supporting bytes"'
        else:
            files[{"add": "_new_registry_fixture.py", "literal": ".py", "hidden": ".hidden.py"}[change]] = ""
        return {}

    monkeypatch.setattr(preflight, "prepare", prepare)
    with pytest.raises(Incomplete, match="source or contract changed"):
        await preflight.check(cases, tmp_path, **options)


@pytest.mark.parametrize("when", ["before", "during"])
async def test_complete_check_refuses_stale_registry_cache(tmp_path, monkeypatch, registry_files, when):
    files, cache = registry_files
    name = next(iter(files))
    files[name] = "@mcp.tool()\ndef qualification_registry_probe(): pass\n"
    cache()
    cases, options = admitted(tmp_path)

    def change():
        files[name] = "@mcp.tool()\ndef changed_registry_probe(): pass\n"

    async def prepare(*args, **kwargs):
        assert when == "during", "stale initial registry reached rendering"
        change()
        return {}

    if when == "before":
        change()
    monkeypatch.setattr(preflight, "prepare", prepare)
    with pytest.raises(Incomplete, match="registry"):
        await preflight.check(cases, tmp_path, **options)


@pytest.mark.parametrize("ignored", ["user_job_tools.py", "nested/fixture.py"])
async def test_complete_check_retains_registry_exclusions_and_matching_cache(
    tmp_path, monkeypatch, registry_files, ignored,
):
    files, cache = registry_files
    cached = cache()
    cases, options = admitted(tmp_path)

    async def prepare(*args, **kwargs):
        files[ignored] = "@mcp.tool()\ndef excluded_registry_probe(): pass\n"
        return {}

    monkeypatch.setattr(preflight, "prepare", prepare)
    for _ in range(2):
        assert (await preflight.check(cases, tmp_path, **options))["complete"]
        assert cache() is cached


@pytest.mark.parametrize("module_name", ["session_config", "types"])
async def test_complete_check_rechecks_builtin_registry_sources(tmp_path, monkeypatch, module_name):
    from genesis.cc import session_config, types

    module = {"session_config": session_config, "types": types}[module_name]
    cases, options = admitted(tmp_path)
    original, changed = preflight.file_hash, False

    def file_hash(path):
        return "0" * 64 if changed and str(path) == module.__file__ else original(path)

    async def prepare(*args, **kwargs):
        nonlocal changed
        changed = True
        return {}

    monkeypatch.setattr(preflight, "file_hash", file_hash)
    monkeypatch.setattr(preflight, "prepare", prepare)
    with pytest.raises(Incomplete, match="source or contract changed"):
        await preflight.check(cases, tmp_path, **options)


@pytest.mark.parametrize("unreadable", ["directory", "file"])
async def test_complete_check_requires_readable_registry_inputs(
    tmp_path, monkeypatch, registry_files, unreadable,
):
    from genesis.learning.procedural import tool_names

    health = Path(tool_names.__file__).resolve().parents[2] / "mcp" / "health"
    iterate, hash_source = Path.iterdir, preflight.file_hash

    def iterdir(path):
        if path == health:
            raise PermissionError("synthetic unreadable registry directory")
        return iterate(path)

    def file_hash(path):
        if Path(path) == health / "_qualification_registry_fixture.py":
            raise PermissionError("synthetic unreadable registry source")
        return hash_source(path)

    monkeypatch.setattr(Path, "iterdir", iterdir if unreadable == "directory" else iterate)
    monkeypatch.setattr(preflight, "file_hash", file_hash if unreadable == "file" else hash_source)
    cases, options = admitted(tmp_path)
    monkeypatch.setattr(preflight, "prepare", AsyncMock(return_value={}))
    with pytest.raises(PermissionError):
        await preflight.check(cases, tmp_path, **options)
