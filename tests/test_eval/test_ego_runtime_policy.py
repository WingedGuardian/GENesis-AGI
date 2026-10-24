"""Canonical policy probes, both explicit orderings and independent reads."""

import json
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from genesis.db.crud import ego
from genesis.eval.ego_runtime.cases import CASES
from genesis.eval.ego_runtime.probe import EFFECT, artifacts, run, run_case


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
@pytest.mark.parametrize("reopen", [False, True], ids=["continuous", "clean-reopen"])
async def test_canonical_case(case, reopen, tmp_path):
    result = await run_case(case, tmp_path / "case", reopen=reopen)
    assert result["status"] == "pass", result
    assert all(step["passed"] for step in result["steps"])


async def test_full_probe_is_disposable_and_complete(tmp_path):
    result = await run(tmp_path)
    assert result["status"] == "pass"
    assert result["passed"] == result["total"] == 18
    assert len({(row["case"], row["writer_mode"]) for row in result["cases"]}) == 18
    assert result["cleanup_confirmed"] is True
    assert list(tmp_path.iterdir()) == []


async def test_noop_claim_is_detected(tmp_path, monkeypatch):
    async def noop(*args, **kwargs):
        return False

    monkeypatch.setattr(ego, "claim_proposal_for_dispatch", noop)
    result = await run_case(CASES[1], tmp_path / "case", reopen=False)
    assert result["status"] == "fail"
    assert result["artifacts"] == []
    assert not result["artifact_check"]


async def test_false_success_without_commit_is_detected(tmp_path, monkeypatch):
    async def noop(*args, **kwargs):
        return True

    monkeypatch.setattr(ego, "resolve_proposal", noop)
    result = await run_case(CASES[1], tmp_path / "case", reopen=True)
    assert result["status"] == "fail"
    assert result["steps"][0]["observed_result"] is True
    assert result["steps"][0]["observed_state"]["status"] == "pending"
    assert result["steps"][0]["passed"] is False


async def test_uncommitted_write_is_not_treated_as_committed(tmp_path, monkeypatch):
    async def uncommitted(db, proposal_id, **kwargs):
        await db.execute("UPDATE ego_proposals SET status = 'approved' WHERE id = ?", (proposal_id,))
        return True

    monkeypatch.setattr(ego, "resolve_proposal", uncommitted)
    case = replace(CASES[0], steps=(CASES[0].steps[0],))
    result = await run_case(case, tmp_path / "case", reopen=False)
    assert result["status"] == "fail"
    assert result["steps"][0]["observed_state"]["status"] == "pending"


async def test_removed_claim_guard_is_detected(tmp_path, monkeypatch):
    async def unguarded(db, proposal_id, **kwargs):
        await db.execute("UPDATE ego_proposals SET status = 'executed' WHERE id = ?", (proposal_id,))
        await db.commit()
        return True

    monkeypatch.setattr(ego, "claim_proposal_for_dispatch", unguarded)
    result = await run_case(CASES[0], tmp_path / "case", reopen=False)
    assert result["status"] == "fail"
    assert len(result["artifacts"]) == 1
    assert not result["artifact_check"]


async def test_cancellation_cleanup(tmp_path, monkeypatch):
    import asyncio

    started = asyncio.Event()

    async def cancelled(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(ego, "resolve_proposal", cancelled)
    task = asyncio.create_task(run(tmp_path))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []


async def test_wrong_expectation_is_detected(tmp_path):
    bad = replace(CASES[0], steps=(replace(CASES[0].steps[0], result=False),))
    result = await run_case(bad, tmp_path / "case", reopen=False)
    assert result["status"] == "fail"
    assert result["steps"][0]["passed"] is False


def test_artifact_bytes_are_independently_read(tmp_path):
    artifact = tmp_path / "effect-1.json"
    artifact.write_bytes(EFFECT)
    first = artifacts(tmp_path)
    artifact.write_bytes(b"incorrect artifact")
    assert artifacts(tmp_path)[0]["sha256"] != first[0]["sha256"]


async def test_corrupt_fixture_output_fails_complete_case(tmp_path, monkeypatch):
    from genesis.eval.ego_runtime import probe

    original = probe.artifacts

    def corrupt_then_observe(root):
        for path in root.glob("effect-*.json"):
            path.write_bytes(b"incorrect output")
        return original(root)

    monkeypatch.setattr(probe, "artifacts", corrupt_then_observe)
    result = await run_case(CASES[1], tmp_path / "case", reopen=False)
    assert result["status"] == "fail"
    assert all(step["passed"] for step in result["steps"])
    assert not result["artifact_check"]


def test_cli_process_e2e(tmp_path):
    from genesis.eval.ego_runtime import probe

    src = Path(probe.__file__).resolve().parents[3]
    launch = ("import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
              "runpy.run_module('genesis.eval.ego_runtime',run_name='__main__')")
    completed = subprocess.run(
        [sys.executable, "-c", launch, str(src), "--temp-root", str(tmp_path)],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["passed"] == report["total"] == 18
    assert report["cleanup_confirmed"]
    assert not list(tmp_path.iterdir())


def test_cli_failure_is_nonzero(tmp_path, monkeypatch, capsys):
    from genesis.eval.ego_runtime.__main__ import main

    async def noop(*args, **kwargs):
        return False

    monkeypatch.setattr(ego, "claim_proposal_for_dispatch", noop)
    assert main(["--temp-root", str(tmp_path)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "fail"
    assert report["passed"] < report["total"]


def test_missing_temp_root_is_incomplete(tmp_path, capsys):
    from genesis.eval.ego_runtime.__main__ import main

    assert main(["--temp-root", str(tmp_path / "missing")]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "incomplete"


def test_old_trace_assessment_cli_is_removed(tmp_path):
    from genesis.eval.ego_runtime.__main__ import main

    with pytest.raises(SystemExit) as exc:
        main([str(tmp_path / "trace.json")])
    assert exc.value.code == 2


async def test_relative_unicode_scratch_parent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parent = Path("scratch # résumé")
    parent.mkdir()
    report = await run(parent)
    assert report["status"] == "pass"
    assert list(parent.iterdir()) == []
