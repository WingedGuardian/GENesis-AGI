"""Runner binding, retained-answer scoring, privacy and read-only failure boundaries."""

import copy
import os

import pytest

from genesis.eval.qualification import __main__ as cli
from genesis.eval.qualification import pinned, run
from genesis.eval.qualification.accounting import Journal
from genesis.eval.qualification.evidence import Incomplete
from tests.test_eval.qualification_runner_fixtures import make_router, runner_data  # noqa: F401
from tests.test_eval.qualification_transport_fixtures import Gateway


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
