"""Runner binding, retained-answer scoring, privacy and read-only failure boundaries."""

import asyncio
import copy
import json
import os

import pytest

from genesis.eval.qualification import __main__ as cli
from genesis.eval.qualification import pinned, preflight, run
from genesis.eval.qualification.accounting import Journal
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_runner_fixtures import (
    LabelGateway,
    finish_acquisition_logging,
    make_router,
    report_cli,
    runner_data,  # noqa: F401
)
from tests.test_eval.qualification_transport_fixtures import Gateway
from tests.test_eval.test_qualification_preflight import registry_files  # noqa: F401


async def test_current_full_coordinate_population_matches_wire_constructor(data):
    run.validate_campaign(data.manifest, data.checked, data.alias)
    assert len(data.manifest["attempts"]) == 6


@pytest.mark.parametrize("field", ["model", "alias", "upstream", "endpoint", "request_hash",
                                 "generation_namespace", "credential_fingerprint", "receipt_provider",
                                 "provider_identity_evidence", "contract", "version", "case_id",
                                 "repetition", "prompt_hash"])
async def test_no_schedule_field_can_change_silently(data, field):
    mutated = copy.deepcopy(data.manifest)
    mutated["attempts"][0][field] = 2 if field == "repetition" else "changed"
    with pytest.raises(Incomplete):
        run.validate_campaign(mutated, data.checked, data.alias)


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "bool_repetition", "wrong_params",
                                  "stale_corpus", "unfunded"])
async def test_full_schedule_and_admission_are_required(data, change):
    manifest = copy.deepcopy(data.manifest)
    if change == "missing":
        manifest["attempts"].pop()
    elif change in ("extra", "duplicate"):
        row = copy.deepcopy(manifest["attempts"][0])
        row["id"] = "extra"
        if change == "extra":
            row["case_id"] = "foreign-case"
        manifest["attempts"].append(row)
        manifest["budget"] = "0.7"
    elif change == "bool_repetition":
        manifest["attempts"][0]["repetition"] = True
    elif change == "wrong_params":
        manifest["binding"]["transports"][data.alias]["params"]["judge"]["max_tokens"] += 1
    elif change == "stale_corpus":
        manifest["binding"]["admission"]["corpus_hash"] = "f" * 64
    else:
        manifest["budget"] = "0.5"
    with pytest.raises(Incomplete):
        run.validate_campaign(manifest, data.checked, data.alias)


async def test_retained_answers_rescore_without_writes_or_key(data, monkeypatch):
    with Journal(data.path, data.manifest) as campaign:
        records, _ = await run.score(data.cases, make_router(data, campaign, True), data.temp)
        assert all(not row["local"] for reps in records.values() for rows in reps.values() for row in rows.values())
    assert len(data.gateway.requests) == 6
    before = {path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
              for path in data.path.iterdir()}
    monkeypatch.delenv(pinned.KEY_ENV)
    data.gateway = Gateway()
    view = run.ReadView(data.path)
    rerun, _ = await run.score(data.cases, make_router(data, view, False), data.temp)
    assert rerun == records and not data.gateway.requests and not data.gateway.key_reads
    assert before == {path.name: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                      for path in data.path.iterdir()}


async def test_missing_answers_are_unscored_without_dispatch(data):
    with Journal(data.path, data.manifest):
        pass
    records, _ = await run.score(data.cases, make_router(data, run.ReadView(data.path), False), data.temp)
    assert {row["local"] for reps in records.values() for rows in reps.values() for row in rows.values()} == {"unanswered"}
    assert not data.gateway.requests


@pytest.mark.parametrize("content", ['broken', '{"score":true}', '{"score":"1"}', '{"score":null}',
                                     '{"score":' + '1' + '0' * 400 + '}'])
async def test_only_verified_malformed_answers_count_as_model_errors(data, content):
    data.gateway.answer = content
    with Journal(data.path, data.manifest) as campaign:
        records, _ = await run.score(data.cases, make_router(data, campaign, True), data.temp)
        assert not campaign.state.blockers
    assert len(data.gateway.requests) == 6
    assert all(row["error"] == "model_output_invalid" and row["local"] is None
               for reps in records.values() for rows in reps.values() for row in rows.values())


async def test_local_post_answer_fault_stops_and_survives_restart(data, monkeypatch):
    actual = run._case

    async def fault(*args):
        await actual(*args)
        raise RuntimeError("private prompt must never be public")

    monkeypatch.setattr(run, "_case", fault)
    with Journal(data.path, data.manifest) as campaign:
        records, _ = await run.score(data.cases, make_router(data, campaign, True), data.temp)
        assert campaign.state.blockers
    assert len(data.gateway.requests) == 1
    assert all(row["local"] for reps in records.values() for rows in reps.values() for row in rows.values())
    with pytest.raises(Incomplete):
        run.ReadView(data.path).ready()


async def test_report_ephemeral_failure_changes_no_history(data):
    with Journal(data.path, data.manifest):
        pass
    before = {p.name: p.read_bytes() for p in data.path.iterdir()}
    view = run.ReadView(data.path)
    view.fail("attempt-0", "private error")
    with pytest.raises(Incomplete, match="campaign_incomplete"):
        view.ready()
    assert before == {p.name: p.read_bytes() for p in data.path.iterdir()}


@pytest.mark.parametrize("module", ["run.py", "__main__.py", "execution.py", "accounting.py", "evidence.py"])
@pytest.mark.parametrize("when", ["before", "after"])
async def test_report_binds_every_direct_runner_source(data, monkeypatch, capsys, module, when):
    # Actual identity and report boundaries; subset admission is explicit here.
    data.gateway = LabelGateway(data)
    with Journal(data.path, data.manifest) as campaign:
        await run.score(data.cases, make_router(data, campaign, True), data.temp)
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
              for p in data.path.iterdir()}
    original_hash, original_score = preflight.file_hash, run.score
    changed = when == "before"

    def file_hash(path):
        if changed and path == run.Path(run.__file__).with_name(module):
            return "0" * 64
        return original_hash(path)

    async def score_then_change(*args, **kwargs):
        nonlocal changed
        assert when == "after", "changed source reached scoring"
        result = await original_score(*args, **kwargs)
        assert all(row["agreed"] for reps in result[0].values()
                   for rows in reps.values() for row in rows.values())
        changed = True
        return result

    monkeypatch.setattr(preflight, "file_hash", file_hash)
    monkeypatch.setattr(run, "score", score_then_change)
    monkeypatch.delenv(pinned.KEY_ENV)
    reads = data.gateway.key_reads, data.gateway.receipt_reads
    await finish_acquisition_logging()
    capsys.readouterr()  # Separate acquisition setup output from the actual CLI.
    assert await asyncio.to_thread(report_cli, data, monkeypatch) == 2
    report = json.loads(capsys.readouterr().out)
    reason = "campaign_incompatible" if when == "before" else "admission_changed"
    assert all(rep["unscored"] == 2 and rep["unscored_reasons"] == [reason]
               for value in report["contracts"].values() for rep in value["repetitions"])
    assert len(data.gateway.requests) == 6
    assert reads == (data.gateway.key_reads, data.gateway.receipt_reads)
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                      for p in data.path.iterdir()}


@pytest.mark.parametrize("change", ["add", "remove", "modify", "literal", "hidden",
                                 "stale_cache", "unchanged", "excluded", "nested"])
@pytest.mark.usefixtures("registry_files")
async def test_report_final_boundary_rebuilds_registry_inventory(
    request, data, monkeypatch, capsys, change,
):
    # Subset CLI plumbing; actual retained-answer scoring and inventory run.
    # Complete corpus admission is covered separately, not asserted here.
    files, cache = request.getfixturevalue("registry_files")
    cached = cache()
    data.gateway = LabelGateway(data)
    with Journal(data.path, data.manifest) as campaign:
        await run.score(data.cases, make_router(data, campaign, True), data.temp)
    assert len(data.gateway.requests) == 6
    requests, key_reads = len(data.gateway.requests), data.gateway.key_reads
    before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
              for p in data.path.iterdir()}
    actual_score = run.score

    async def score_then_change(*args, **kwargs):
        scored = await actual_score(*args, **kwargs)
        assert all(row["agreed"] for reps in scored[0].values()
                   for rows in reps.values() for row in rows.values())
        if change == "remove":
            files.clear()
        elif change == "modify":
            files[next(iter(files))] = '"changed supporting bytes"'
        elif change == "stale_cache":
            files[next(iter(files))] = "@mcp.tool()\ndef changed_runner_probe(): pass\n"
        elif change != "unchanged":
            files[{
                "add": "_runner_registry_fixture.py", "literal": ".py", "hidden": ".hidden.py",
                "excluded": "user_job_tools.py", "nested": "nested/fixture.py",
            }[change]] = ""
        return scored

    monkeypatch.setattr(run, "score", score_then_change)
    monkeypatch.delenv(pinned.KEY_ENV)
    await finish_acquisition_logging()
    capsys.readouterr()  # Only the following CLI invocation must be one JSON result.
    assert await asyncio.to_thread(report_cli, data, monkeypatch) == 2
    report = json.loads(capsys.readouterr().out)
    repetitions = [rep for value in report["contracts"].values() for rep in value["repetitions"]]
    if change in {"unchanged", "excluded", "nested"}:
        assert all(rep["correct"] == 2 and rep["unscored"] == 0 for rep in repetitions)
    else:
        assert all(rep["unscored"] == 2 and rep["unscored_reasons"] == ["admission_changed"]
                   for rep in repetitions)
    assert cache() is cached  # Refusal must never repair the stale cache.
    assert len(data.gateway.requests) == requests and data.gateway.key_reads == key_reads
    assert before == {p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
                      for p in data.path.iterdir()}


def test_public_financial_boundary_has_no_declared_proof_bypass():
    with pytest.raises(Incomplete, match="financial_eligibility_unavailable"):
        run._paid_eligibility({"proof": True, "budget": "5"}, approved=True)


def test_cli_never_echoes_private_exception_or_sdk_output(monkeypatch, capsys, tmp_path):
    async def failure(_args):
        print("private SDK answer")
        raise ValueError("private source prompt sk-synthetic-secret")

    monkeypatch.setattr(cli, "run", failure)
    assert cli.main(["review-references", "--spec", str(tmp_path / "private")]) == 2
    output = capsys.readouterr()
    assert "private" not in output.out and "secret" not in output.out and not output.err
    assert "admission_or_processing_failed" in output.out


@pytest.mark.parametrize("kind", ["missing", "missing_lock", "live_writer", "torn", "hardlink", "symlink"])
async def test_read_only_refusals_preserve_all_existing_evidence(data, kind):
    if kind == "missing":
        with pytest.raises((Incomplete, OSError)):
            run.ReadView(data.path)
        assert not data.path.exists()
        return
    with Journal(data.path, data.manifest):
        if kind == "live_writer":
            with pytest.raises((Incomplete, OSError)):
                run.ReadView(data.path)
            return
    history = data.path / "answers.jsonl"
    if kind == "missing_lock":
        (data.path / "campaign.lock").unlink()
    elif kind == "torn":
        with history.open("ab") as target:
            target.write(b'{"unfinished":')
    elif kind == "hardlink":
        os.link(history, data.temp / "other-link")
    elif kind == "symlink":
        moved = data.temp / "original-history"
        history.rename(moved)
        history.symlink_to(moved)
    before = history.read_bytes()
    with pytest.raises((Incomplete, OSError)):
        run.ReadView(data.path).ready()
    assert history.read_bytes() == before
    if kind == "missing_lock":
        assert not (data.path / "campaign.lock").exists()
