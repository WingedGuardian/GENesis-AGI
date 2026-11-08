"""The reader must reject incomplete and stale evidence, including whole-case loss."""

import copy
import json
import os
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from genesis.eval.qualification.evidence import digest
from scripts.ci.codex_qualification_readback import (
    ACCEPTED_STEER,
    inventory,
    readback,
    required_cases,
)


@pytest.fixture
def packet(tmp_path):
    cases = sorted(required_cases())
    rows = {}
    for index, nodeid in enumerate(cases):
        name = f"case-{index}/compat-evidence.json"
        path = tmp_path / name
        path.parent.mkdir()
        evidence = {"fixture_consent": True, "production_ready": False}
        thread, turn = "fixture-thread", "fixture-turn"
        terminal = {"method": "turn/completed", "params": {"threadId": thread, "turn": {"id": turn, "status": "completed"}}}
        evidence.update(thread=thread, turn=turn, events=[terminal], model_requests=[{"fixture": "action"}, {"fixture": "final"}])
        if "::test_native_approval[" in nodeid:
            decision = nodeid.rsplit("[", 1)[1][:-1]
            evidence.update(decision=decision, sink=decision == "accept")
            evidence["events"].insert(0, {"id": 8, "method": "item/commandExecution/requestApproval",
                "params": {"threadId": thread, "turnId": turn, "command": "printf fixture > approval.txt"}})
            if decision in {"interrupt", "stale"}:
                terminal["params"]["turn"]["status"] = "interrupted"
            elif decision == "disconnect":
                evidence["events"].pop()
                evidence["eof_exit"] = {"code": 0, "before_forced_cleanup": True}
            elif decision == "steer":
                evidence["steer"] = {"turnId": turn}
                evidence["model_requests"][-1] = {"input": ACCEPTED_STEER}
            if decision == "accept":
                (path.parent / "workspace").mkdir()
                (path.parent / "workspace/approval.txt").write_text("fixture")
        elif "::test_canonical_ask_route[" in nodeid:
            evidence["client"] = "cli" if nodeid.endswith("-cli]") else "app"
            if evidence["client"] == "cli":
                evidence["events"] = [{"type": "turn.completed"}]
            evidence["sink"] = "[adapted-ask-" not in nodeid
            (path.parent / "guard-result.json").write_text(json.dumps({
                "code": 0, "stdout": json.dumps({"hookSpecificOutput": {"permissionDecision": "ask"}}),
            }))
            if evidence["sink"]:
                (path.parent / "workspace").mkdir()
                (path.parent / "workspace/canonical-receipt.txt").write_text("fixture")
        elif nodeid.endswith("[provenance-cleanup]"):
            evidence.update(cleanup_confirmed=True, child_before={"pid": 100, "start": 99, "state": "S"},
                            child_after={"state": "absent"}, owned_ancestry=[100, 50], driver_pid=50)
            (path.parent / "marker").write_text("1")
        elif nodeid.endswith("[required-failure]"):
            event = {"id": 2, "error": {"code": -32000, "message": "fixture required MCP failure"}}
            evidence.update(events=[event], failure_response=event, model_requests=[])
        elif nodeid.endswith("::test_native_resume"):
            response = {"id": 2, "result": {"thread": {"id": thread}}}
            evidence.update(resume=response["result"], resume_events=[response])
            (path.parent / "workspace").mkdir()
            (path.parent / "workspace/resume.txt").write_text("fixture")
        else:
            evidence.update(versions={"cli": "codex-cli 0.166.0", "app": "codex-cli 0.166.0"},
                            client="cli" if nodeid.endswith("-cli]") else "app", requests=[{}, {}])
            if evidence["client"] == "cli":
                evidence["events"] = [{"type": "turn.completed"}]
        evidence["files"] = inventory(path.parent, (".",))
        path.write_text(json.dumps(evidence))
        rows[nodeid] = {"phases": [[phase, "passed"] for phase in ("setup", "call", "teardown")], "receipt": name}
    manifest = {"schema": "codex-compatibility/v1", "head": "current-head", "base": "current-base", "cases": cases}
    report = {"exitstatus": 0, "cases": rows, "artifacts": inventory(tmp_path, (".",)), "manifest_digest": digest(manifest)}
    return manifest, report, tmp_path


def result(packet):
    manifest, report, root = packet
    return readback(manifest, report, root, head="current-head", base="current-base")


def test_complete_mechanism_measurement_never_means_production_ready(packet):
    observed = result(packet)
    assert observed["status"] == "complete" and observed["case_total"] == len(required_cases())
    assert observed["production_ready"] is False
    assert observed["canonical_ask_native_wire"] == "unsupported"
    assert observed["canonical_same_session_approval"] == "unproven"


@pytest.mark.parametrize("fault", ["head", "base", "schema", "case", "duplicate", "both-census", "exit", "bool-exit", "phase", "skip", "missing", "tamper", "receipt", "duplicate-receipt", "malformed", "identity"])
def test_incomplete_or_altered_packet_refuses(packet, fault):
    manifest, report, root = copy.deepcopy(packet)
    key = next(iter(report["cases"]))
    if fault in {"head", "base", "schema"}:
        manifest[fault] = "old"
    elif fault == "case":
        del report["cases"][key]
    elif fault == "duplicate":
        manifest["cases"].append(manifest["cases"][0])
    elif fault == "both-census":
        manifest["cases"].remove(key)
        del report["cases"][key]
    elif fault in {"exit", "bool-exit"}:
        report["exitstatus"] = 1 if fault == "exit" else False
    elif fault == "phase":
        report["cases"][key]["phases"].pop()
    elif fault == "skip":
        report["cases"][key]["phases"][1][1] = "skipped"
    elif fault in {"missing", "tamper", "malformed"}:
        path = root / report["cases"][key]["receipt"]
        if fault == "missing":
            path.unlink()
        else:
            path.write_text("changed" if fault == "tamper" else '{"x":1,"x":2}')
            if fault == "malformed":
                report["artifacts"] = inventory(root, (".",))
    elif fault == "receipt":
        report["cases"][key]["receipt"] = "../outside"
    elif fault == "duplicate-receipt":
        second = list(report["cases"])[1]
        report["cases"][second]["receipt"] = report["cases"][key]["receipt"]
    else:
        report["manifest_digest"] = "different-native-package"
    if fault in {"duplicate", "both-census"}:
        report["manifest_digest"] = digest(manifest)  # census independently refuses even coherent shortening
    with pytest.raises(ValueError):
        result((manifest, report, root))


def test_sink_disagreement_rejects_even_if_ordinary_hashes_are_refreshed(packet):
    manifest, report, root = packet
    key = next(key for key in report["cases"] if "::test_native_approval[accept]" in key)
    receipt = root / report["cases"][key]["receipt"]
    (receipt.parent / "workspace/approval.txt").write_text("wrong effect")
    evidence = json.loads(receipt.read_text())
    evidence["files"] = {key: value for key, value in inventory(receipt.parent, (".",)).items() if key != receipt.name}
    receipt.write_text(json.dumps(evidence))
    report["artifacts"] = inventory(root, (".",))
    with pytest.raises(ValueError, match="native effect"):
        result(packet)


def test_non_receipt_artifact_change_refuses(packet):
    _, report, root = packet
    log = root / "controller.log"
    log.write_text("original fixture log")
    report["artifacts"] = inventory(root, (".",))
    log.write_text("changed fixture log")
    with pytest.raises(ValueError, match="changed native artifacts"):
        result(packet)


@pytest.mark.parametrize("fault", ["events", "terminal", "approval-turn", "resume", "replay", "required-response", "required-work", "cleanup", "steer", "eof"])
def test_missing_or_contradictory_native_witness_refuses(packet, fault):
    _, report, root = packet
    target = {"events": "test_native_approval[accept]", "terminal": "test_native_approval[accept]",
              "approval-turn": "test_native_approval[accept]", "resume": "test_native_resume",
              "replay": "test_native_resume", "required-response": "test_required_mcp_and_owned_cleanup[required-failure]",
              "required-work": "test_required_mcp_and_owned_cleanup[required-failure]",
              "cleanup": "test_required_mcp_and_owned_cleanup[provenance-cleanup]",
              "steer": "test_native_approval[steer]", "eof": "test_native_approval[disconnect]"}[fault]
    key = next(key for key in report["cases"] if key.endswith("::" + target))
    receipt = root / report["cases"][key]["receipt"]
    evidence = json.loads(receipt.read_text())
    if fault == "events":
        evidence["events"] = []
    elif fault == "terminal":
        evidence["events"][-1]["params"]["turn"]["status"] = "failed"
    elif fault == "approval-turn":
        evidence["events"][0]["params"]["turnId"] = "wrong-turn"
    elif fault == "resume":
        evidence["resume"]["thread"]["id"] = "wrong-thread"
    elif fault == "replay":
        evidence["model_requests"].append({"fixture": "replayed"})
    elif fault == "required-response":
        evidence["failure_response"] = {"id": 2, "result": {}}
    elif fault == "required-work":
        evidence["model_requests"] = [{"fixture": "unpermitted work"}]
    elif fault == "cleanup":
        evidence["child_after"] = {"state": "S", "start": 99}
    elif fault == "steer":
        evidence["model_requests"][-1] = {"input": "steer was lost"}
    else:
        del evidence["eof_exit"]
    receipt.write_text(json.dumps(evidence))
    report["artifacts"] = inventory(root, (".",))
    with pytest.raises(ValueError):
        result(packet)


def test_workflow_preserves_hidden_files_and_directory_links(tmp_path):
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/codex-qualification.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["contained"]["steps"]
    pack = [step for step in steps if step.get("name") == "Pack complete evidence"]
    assert len(pack) == 1, "workflow must preserve hidden files and link shape in an evidence archive"
    root = tmp_path / "codex-evidence"
    hidden = root / "case/.claude/hooks/guard.py"
    hidden.parent.mkdir(parents=True)
    hidden.write_text("synthetic fixture bytes")
    (root / "case-current").symlink_to("case", target_is_directory=True)
    (root / "file-link").symlink_to("case/.claude/hooks/guard.py")
    before = inventory(root, (".",))
    subprocess.run(["bash", "-euo", "pipefail", "-c", pack[0]["run"]], check=True,
                   env={"PATH": os.environ["PATH"], "RUNNER_TEMP": str(tmp_path)})
    restored = tmp_path / "download"
    with tarfile.open(tmp_path / "codex-evidence.tar.gz") as archive:
        archive.extractall(restored, filter="data")
    assert inventory(restored, (".",)) == before
    assert (restored / "case-current").is_symlink()
    assert (restored / "file-link").is_symlink()
