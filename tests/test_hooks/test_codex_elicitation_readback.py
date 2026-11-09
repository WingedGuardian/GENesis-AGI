"""Synthetic raw-artifact controls for the independent reader; no native agent."""

import copy
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.eval.qualification.evidence import digest
from scripts.ci.codex_elicitation_readback import (
    CASES,
    META,
    PREFIX,
    check_case,
    readback,
    response_for,
)
from scripts.ci.codex_qualification_readback import inventory


def test_contained_reader_entry_works_with_the_images_python_path(tmp_path):
    root = Path(__file__).resolve().parents[2]
    script = (root / "scripts/ci/codex_elicitation.sh").read_text()
    entries = [line for line in script.splitlines() if line.startswith("python ") and "codex_elicitation_readback" in line]
    assert len(entries) == 1
    command = shlex.split(entries[0].rstrip("\\").strip())
    # Limit this entry probe to the two known reader forms before executing
    # anything; --help must never launch native tests or another program.
    assert command in (["python", "scripts/ci/codex_elicitation_readback.py"],
                       ["python", "-m", "scripts.ci.codex_elicitation_readback"])
    result = subprocess.run([sys.executable, *command[1:], "--help"], cwd=root,
                            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path),
                                 "PYTHONPATH": str(root / "src"), "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    assert "--run-id" in result.stdout and "--attempt" in result.stdout


def persist(root, evidence, sinks):
    (root / "server.jsonl").write_text("".join(json.dumps(row) + "\n" for row in evidence["server"]))
    for name, rows in sinks.items():
        if rows:
            (root / f"{name}-sink.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    evidence["files"] = {name: value for name, value in inventory(root, (".",)).items()
                         if name != "elicitation-evidence.json"}
    (root / "elicitation-evidence.json").write_text(json.dumps(evidence))


def golden(root, case):
    root.mkdir(exist_ok=True)
    multi = case in {"approval_two_requests", "approval_stale_after_interrupt"}
    count = 2 if multi else 1
    never = case in {"approval_never", "data_form_never", "data_form_empty"}
    interrupted = case in {"approval_interrupt", "approval_wrong_rpc_id", "approval_stale_after_interrupt"}
    no_ask = case in {"approval_never", "approval_unattended", "data_form_empty"}
    normal = not (interrupted or no_ask or case == "approval_eof")
    positive = case in {"approval_accept_true", "approval_duplicate_reply", "approval_two_requests", "data_form_never"}
    started = {"thread": {"id": "thread"}, "approvalPolicy": "never" if never else "on-request",
               "sandbox": {"type": "dangerFullAccess" if never else "readOnly"}}
    evidence = {"schema": "codex-elicitation/v1", "case": case, "fixture_consent": True,
                "production_ready": False, "thread": "thread", "turns": [f"turn-{i}" for i in range(count)],
                "started": started, "events": [{"id": 2, "result": started}], "sent": [],
                "observed": [], "observations": [], "server": [], "driver_pid": 10,
                "driver_before": {"pid": 10, "start": 10, "ppid": 9, "pgid": 10, "sid": 10, "state": "S"},
                "owned": [{"before": {"pid": 20, "start": 20, "ppid": 10, "state": "S"},
                           "after": {"state": "absent"}, "ancestry": [20, 10]}],
                "exit_code": 0,
                "model_requests": [{}] * (count if interrupted or case == "approval_eof" else count * 2),
                "trust": {"effective_config": {"approvals_reviewer": "user", "mcp_servers": {
                    "fixture": {"default_tools_approval_mode": "approve"}}}}}
    sinks = {"approval": [], "data": []}

    def snapshot(label, at):
        evidence["observations"].append({"label": label, "at": at, **{
            name: "".join(json.dumps(row) + "\n" for row in rows).encode().hex() if rows else None
            for name, rows in sinks.items()}})

    for index, turn in enumerate(evidence["turns"]):
        base = index * 1000
        nonce = f"{index + 1:032x}"
        common = {"case": case, "nonce": nonce, "incarnation": "b" * 32}

        def server(event, at, *, common=common, base=base, **values):
            row = {**common, "event": event, "at": base + at, **values}
            evidence["server"].append(row)
            return row

        server("entered", 100, process={"pid": 20, "start": 20}, protocol="2025-06-18",
               capabilities={"elicitation": {}}, unattended=case == "approval_unattended")
        if case == "approval_unattended":
            server("role_refused", 110)
        else:
            field = "value" if case == "data_form_never" else "confirmed"
            schema = {"type": "object", "properties": {field: {"type": "boolean"}}, "required": [field]}
            if case == "data_form_empty":
                schema = {"type": "object", "properties": {}}
            params = {"mode": "form", "message": f"Fixture approval {nonce}: append one row to "
                      + ("data-sink.jsonl" if case == "data_form_never" else "approval-sink.jsonl")
                      + " in this isolated fixture.", "requestedSchema": schema}
            if case not in {"data_form_never", "data_form_empty"}:
                params["_meta"] = copy.deepcopy(META)
            if case == "data_form_never":
                server("data_requested", 110, message=params["message"])
            else:
                server("requested", 110, wire={"method": "elicitation/create", "params": copy.deepcopy(params)})
            if not no_ask:
                ask = {"id": index + 200, "method": "mcpServer/elicitation/request", "params": {
                    **copy.deepcopy(params), "threadId": "thread", "turnId": turn, "serverName": "fixture"}}
                evidence["events"].append(ask)
                evidence["observed"].append({"at": base + 120, "message": copy.deepcopy(ask)})
                snapshot("pending-ask", base + 125)
            if normal:
                snapshot("before-response", base + 130)
                result = response_for(case)
                evidence["sent"].append({"at": base + 140, "message": {
                    "id": index + 200, "result": result}, "synthetic_frontend": True})
                server("response", 150, response={key: value for key, value in result.items() if value is not None})
                if positive:
                    name = "data" if case == "data_form_never" else "approval"
                    sinks[name].append({**common, "at": base + 160})
                    server("appended", 170, sink=f"{name}-sink.jsonl")
                else:
                    server("refused", 170)
            elif case == "data_form_empty":
                server("response", 150, response={"action": "accept", "content": {}})
                server("refused", 170)
            elif case == "approval_never":
                server("response", 150, response={"action": "decline"})
                server("refused", 170)
            elif case != "approval_eof":
                server("cancelled", 170)
        if interrupted and (case == "approval_interrupt" or case == "approval_stale_after_interrupt" and index == 0):
            snapshot("before-interrupt", base + 180)
            evidence["sent"].append({"at": base + 190, "message": {"id": 400 + index, "method": "turn/interrupt",
                "params": {"threadId": "thread", "turnId": turn}}})
            snapshot("after-interrupt", base + 210)
        if case != "approval_eof":
            evidence["events"].append({"method": "turn/completed", "params": {"threadId": "thread", "turn": {
                "id": turn, "status": "interrupted" if interrupted else "completed"}}})
    if case == "approval_eof":
        evidence["eof_exit"] = {"code": 0, "before_forced_cleanup": True}
    if case in {"approval_wrong_rpc_id", "approval_duplicate_reply", "approval_stale_after_interrupt"}:
        if case == "approval_duplicate_reply":
            snapshot("before-duplicate", 2400)
        snapshot("before-negative-injection", 2490)
        evidence["sent"].extend([
            {"at": 2500, "message": {"id": "foreign" if case == "approval_wrong_rpc_id" else 200,
                                      "result": {"action": "accept", "content": {"confirmed": True}}},
             "negative_injection": True},
            {"at": 2600, "message": {"id": 100, "method": "config/read", "params": {}}},
        ])
        evidence["events"].append({"id": 100, "result": {}})
        snapshot("after-negative-barrier", 2650)
        if interrupted:
            snapshot("before-interrupt", 2700)
            evidence["sent"].append({"at": 2800, "message": {"id": 500, "method": "turn/interrupt",
                "params": {"threadId": "thread", "turnId": evidence["turns"][-1]}}})
            snapshot("after-interrupt", 2900)
    evidence["events"].append({"closed": True})
    snapshot("before-close", 9000)
    snapshot("after-close", 10000)
    snapshot("after-retirement", 11000)
    persist(root, evidence, sinks)
    return evidence, sinks


@pytest.mark.parametrize("case", CASES)
def test_complete_synthetic_raw_witnesses_are_readable(tmp_path, case):
    evidence, _ = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("damage", [
    "early_write", "late_write", "duplicate", "missing_ask", "wrong_turn", "bool_id", "unmarked",
    "default_true", "persistence", "model_reply", "missing_terminal", "survivor", "missing_start",
    "unowned", "forced_kill", "modern", "no_capability", "lost_order", "empty_snapshot", "wrong_response",
    "wrong_server_response", "missing_server_effect", "unknown_nonce", "hidden_extra_file",
])
def test_semantic_damage_is_refused_even_after_artifacts_are_rehashed(tmp_path, damage):
    case = "approval_accept_true"
    evidence, sinks = golden(tmp_path, case)
    ask = evidence["events"][1]
    wire = evidence["server"][1]["wire"]
    if damage == "early_write":
        sinks["approval"][0]["at"] = 139
    elif damage == "late_write":
        sinks["approval"][0]["at"] = 12000
    elif damage == "duplicate":
        sinks["approval"].append(copy.deepcopy(sinks["approval"][0]))
    elif damage == "missing_ask":
        evidence["events"].pop(1)
    elif damage == "wrong_turn":
        ask["params"]["turnId"] = "foreign"
        evidence["observed"][0]["message"] = copy.deepcopy(ask)
    elif damage == "bool_id":
        evidence["sent"][0]["message"]["id"] = True
    elif damage == "unmarked":
        wire["params"].pop("_meta")
    elif damage == "default_true":
        wire["params"]["requestedSchema"]["properties"]["confirmed"]["default"] = True
    elif damage == "persistence":
        evidence["sent"][0]["message"]["result"]["_meta"] = {"persist": "session"}
    elif damage == "model_reply":
        evidence["sent"][0].pop("synthetic_frontend")
    elif damage == "missing_terminal":
        evidence["events"] = [row for row in evidence["events"] if row.get("method") != "turn/completed"]
    elif damage == "survivor":
        evidence["owned"][0]["after"] = {"pid": 20, "start": 20, "state": "S"}
    elif damage == "missing_start":
        evidence["owned"][0]["after"] = {"pid": 20, "state": "Z"}
    elif damage == "unowned":
        evidence["owned"][0]["ancestry"][-1] = 99
    elif damage == "forced_kill":
        evidence["exit_code"] = -9
    elif damage == "modern":
        evidence["server"][0]["protocol"] = "2026-07-28"
    elif damage == "no_capability":
        evidence["server"][0]["capabilities"] = {}
    elif damage == "lost_order":
        evidence["observations"] = []
    elif damage == "empty_snapshot":
        evidence["observations"][-1]["approval"] = None
    elif damage == "wrong_response":
        evidence["sent"][0]["message"]["result"]["content"]["confirmed"] = 1
    elif damage == "wrong_server_response":
        evidence["server"][2]["response"]["content"]["confirmed"] = 1
    elif damage == "missing_server_effect":
        evidence["server"].pop()
    elif damage == "unknown_nonce":
        sinks["approval"][0]["nonce"] = "f" * 32
    else:
        (tmp_path / "unrecorded.txt").write_text("unexpected")
        with pytest.raises(ValueError):
            check_case(case, tmp_path, evidence)
        return
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


def population(root):
    cases = {}
    for case in CASES:
        golden(root / case, case)
        cases[f"{PREFIX}[{case}]"] = {"phases": [[phase, "passed"] for phase in ("setup", "call", "teardown")],
                                    "receipt": f"{case}/elicitation-evidence.json"}
    manifest = {"schema": "codex-elicitation/v1", "head": "head", "base": "base", "run_id": "123", "attempt": "1", "cases": list(cases)}
    report = {"cases": cases, "exitstatus": 0, "artifacts": inventory(root, (".",)), "manifest_digest": digest(manifest)}
    return manifest, report


@pytest.mark.parametrize("damage", ["early_removed", "late_sink_and_log", "late_log", "early_pending", "erased_duplicate_baseline"])
def test_all_sink_snapshots_must_agree_with_chronological_append_only_effects(tmp_path, damage):
    case = "approval_decline" if damage == "early_removed" else "approval_duplicate_reply" if damage == "erased_duplicate_baseline" else "approval_accept_true"
    evidence, sinks = golden(tmp_path, case)
    if damage == "early_removed":
        evidence["observations"][0]["approval"] = b'{"forbidden_early_write":true}\n'.hex()
    elif damage in {"late_sink_and_log", "late_log"}:
        evidence["server"][-1]["at"] = 21000
        if damage == "late_sink_and_log":
            sinks["approval"][0]["at"] = 20000
            value = "".join(json.dumps(row) + "\n" for row in sinks["approval"]).encode().hex()
            for row in evidence["observations"][-2:]:
                row["approval"] = value
    elif damage == "early_pending":
        evidence["observations"][0]["at"] = 100
    else:
        next(row for row in evidence["observations"] if row["label"] == "before-duplicate")["approval"] = None
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("damage", ["pending_missing", "response_before_pending", "raw_missing", "barrier_missing",
                                    "raw_wrongorder", "interrupt_missing", "interrupt_wrongturn", "interrupt_early", "stdout_open"])
def test_lifecycle_observations_are_bound_to_actual_protocol_operations(tmp_path, damage):
    case = "approval_accept_true" if damage == "response_before_pending" else "approval_wrong_rpc_id"
    evidence, sinks = golden(tmp_path, case)
    if damage in {"pending_missing", "raw_missing", "barrier_missing", "interrupt_missing"}:
        label = {"pending_missing": "pending-ask", "raw_missing": "before-negative-injection",
                 "barrier_missing": "after-negative-barrier", "interrupt_missing": "after-interrupt"}[damage]
        evidence["observations"] = [row for row in evidence["observations"] if row["label"] != label]
    elif damage == "response_before_pending":
        next(row for row in evidence["observations"] if row["label"] == "before-response")["at"] = 121
    elif damage == "raw_wrongorder":
        next(row for row in evidence["observations"] if row["label"] == "before-negative-injection")["at"] = 2601
    elif damage == "interrupt_wrongturn":
        next(row for row in evidence["sent"] if row["message"].get("method") == "turn/interrupt")["message"]["params"]["turnId"] = "foreign"
    elif damage == "interrupt_early":
        next(row for row in evidence["sent"] if row["message"].get("method") == "turn/interrupt")["at"] = 120
    else:
        evidence["events"].pop()
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("case,damage", [
    ("approval_never", "accepted"), ("approval_never", "unmarked"),
    ("approval_unattended", "role"), ("approval_unattended", "hidden_request"),
    ("approval_interrupt", "accepted"), ("approval_eof", "unobserved_exit"),
    ("approval_stale_after_interrupt", "current_reply"), ("approval_stale_after_interrupt", "early_replay"),
    ("approval_duplicate_reply", "foreign_reply"), ("approval_two_requests", "reused_id"),
    ("data_form_never", "authority"), ("data_form_empty", "marked"),
])
def test_negative_and_data_controls_cannot_be_promoted_to_authority(tmp_path, case, damage):
    evidence, sinks = golden(tmp_path, case)
    if damage == "accepted":
        if case == "approval_interrupt":
            evidence["server"][-1:] = [
                {**evidence["server"][-1], "event": "response", "response": {"action": "accept", "content": {"confirmed": True}}},
                {**evidence["server"][-1], "event": "refused", "at": 180},
            ]
        else:
            evidence["server"][2]["response"]["action"] = "accept"
    elif damage == "unmarked":
        evidence["server"][1]["wire"]["params"].pop("_meta")
    elif damage == "role":
        evidence["server"][0]["unattended"] = False
    elif damage == "hidden_request":
        evidence["server"][1]["event"] = "requested"
    elif damage == "unobserved_exit":
        evidence["eof_exit"]["before_forced_cleanup"] = False
    elif damage in {"current_reply", "foreign_reply"}:
        evidence["sent"][-2]["message"]["id"] = 201 if damage == "current_reply" else "foreign"
    elif damage == "early_replay":
        evidence["sent"][-2]["at"] = 121
    elif damage == "reused_id":
        evidence["observed"][1]["message"]["id"] = 200
        evidence["events"][3]["id"] = 200
    elif damage == "authority":
        sinks["approval"] = [dict(sinks["data"][0])]
    else:
        evidence["server"][1]["wire"]["params"]["_meta"] = META
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("damage", [None, "missing", "extra", "duplicate", "skipped", "failed", "partial", "run", "attempt", "head", "base", "hash", "artifact", "receipt"])
def test_closed_population_and_phase_accounting(tmp_path, damage):
    manifest, report = population(tmp_path)
    nodeid = next(iter(report["cases"]))
    if damage == "missing":
        manifest["cases"].pop()
    elif damage == "extra":
        manifest["cases"].append("extra")
    elif damage == "duplicate":
        manifest["cases"].append(nodeid)
    elif damage in {"skipped", "failed"}:
        report["cases"][nodeid]["phases"][1][1] = damage
    elif damage == "partial":
        report["cases"][nodeid]["phases"].pop()
    elif damage in {"head", "base", "attempt"}:
        manifest[damage] = "stale"
    elif damage == "run":
        manifest["run_id"] = "stale"
    elif damage == "artifact":
        report["artifacts"].popitem()
    elif damage == "receipt":
        report["cases"][nodeid]["receipt"] = "../foreign.json"
    report["manifest_digest"] = "bad" if damage == "hash" else digest(manifest)
    if damage:
        with pytest.raises(ValueError):
            readback(manifest, report, tmp_path, head="head", base="base", run_id="123", attempt="1")
    else:
        result = readback(manifest, report, tmp_path, head="head", base="base", run_id="123", attempt="1")
        assert result["case_total"] == 17 and result["production_ready"] is False
        assert result["human_frontend"] == "unmeasured"
