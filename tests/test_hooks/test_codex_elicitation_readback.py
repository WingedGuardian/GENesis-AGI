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
    no_ask = case in {"approval_never", "approval_unattended", "data_form_empty"}
    positive = case in {"approval_accept_true", "approval_duplicate_reply", "approval_two_requests", "data_form_never"}
    started = {"thread": {"id": "thread"}, "approvalPolicy": "never" if never else "on-request",
               "sandbox": {"type": "dangerFullAccess" if never else "readOnly"}}
    evidence = {"schema": "codex-elicitation/v1", "case": case, "fixture_consent": True,
                "production_ready": False, "thread": "thread", "turns": [f"turn-{i}" for i in range(count)],
                "started": started, "events": [{"id": 2, "result": started}], "sent": [],
                "observed": [], "observations": [], "server": [], "completions": [], "driver_pid": 10,
                "driver_before": {"pid": 10, "start": 10, "ppid": 9, "pgid": 10, "sid": 10, "state": "S"},
                "owned": [{"before": {"pid": 20, "start": 20, "ppid": 10, "state": "S"},
                           "after": {"state": "absent"}, "ancestry": [20, 10]}],
                "exit_code": 0,
                "model_requests": [{}] * (3 if case == "approval_stale_after_interrupt" else
                                       1 if case in {"approval_interrupt", "approval_eof"} else count * 2),
                "trust": {"effective_config": {"approvals_reviewer": "user", "mcp_servers": {
                    "fixture": {"default_tools_approval_mode": "approve"}}}}}
    evidence["sent"] = [
        {"at": 20, "message": {"id": 1, "method": "initialize", "params": {
            "capabilities": {"experimentalApi": True,
                             "extensions": {"openai/standard-form-input": {}}}}}},
        {"at": 30, "message": {"id": 2, "method": "thread/start", "params": {
            "cwd": str(root), "ephemeral": True, "threadSource": "user"}}},
    ]
    sinks = {"approval": [], "data": []}
    clock = 100

    def tick():
        nonlocal clock
        clock += 10
        return clock

    def snapshot(label):
        evidence["observations"].append({"label": label, "at": tick(), **{
            name: "".join(json.dumps(row) + "\n" for row in rows).encode().hex() if rows else None
            for name, rows in sinks.items()}})

    def inject(request_id):
        snapshot("before-negative-injection")
        evidence["sent"].append({"at": tick(), "message": {"id": request_id,
            "result": {"action": "accept", "content": {"confirmed": True}}}, "negative_injection": True})
        evidence["sent"].append({"at": tick(), "message": {"id": 100, "method": "config/read", "params": {}}})
        evidence["events"].append({"id": 100, "result": {}})
        snapshot("after-negative-barrier")

    def completed(turn, status):
        message = {"method": "turn/completed", "params": {
            "threadId": "thread", "turn": {"id": turn, "status": status}}}
        evidence["completions"].append({"at": tick(), "message": copy.deepcopy(message)})
        evidence["events"].append(message)

    # Literal oracle policy; never derive expected replies/outcomes from readback.
    replies = {
        "approval_decline": {"action": "decline", "content": None},
        "approval_cancel": {"action": "cancel", "content": None},
        "approval_wrong_rpc_id": {"action": "decline", "content": None},
        "approval_stale_after_interrupt": {"action": "decline", "content": None},
        "approval_false": {"action": "accept", "content": {"confirmed": False}},
        "approval_bad_boolean": {"action": "accept", "content": {"confirmed": "true"}},
        "approval_extra_content": {"action": "accept", "content": {"confirmed": True, "extra": True}},
        "approval_missing_content": {"action": "accept"},
        "data_form_never": {"action": "accept", "content": {"value": True}},
    }
    for index, turn in enumerate(evidence["turns"]):
        nonce = f"{index + 1:032x}"
        common = {"case": case, "nonce": nonce, "incarnation": "b" * 32}

        def server(event, *, common=common, **values):
            row = {**common, "event": event, "at": tick(), **values}
            evidence["server"].append(row)
            return row

        server("entered", process={"pid": 20, "start": 20}, protocol="2025-06-18",
               capabilities={"elicitation": {}}, unattended=case == "approval_unattended")
        interrupted = case == "approval_interrupt" or case == "approval_stale_after_interrupt" and index == 0
        if case == "approval_unattended":
            server("role_refused")
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
                server("data_requested", message=params["message"])
            else:
                server("requested", wire={"method": "elicitation/create", "params": copy.deepcopy(params)})
            if not no_ask:
                ask = {"id": index + 200, "method": "mcpServer/elicitation/request", "params": {
                    **copy.deepcopy(params), "threadId": "thread", "turnId": turn, "serverName": "fixture"}}
                evidence["events"].append(ask)
                evidence["observed"].append({"at": tick(), "message": copy.deepcopy(ask)})
                snapshot("pending-ask")
            if case == "approval_eof":
                evidence["eof_live"] = {"at": tick(), "driver": copy.deepcopy(evidence["driver_before"]),
                    "writers": [copy.deepcopy(evidence["owned"][0]["before"])],
                    "ancestries": [[copy.deepcopy(evidence["owned"][0]["before"]),
                                    copy.deepcopy(evidence["driver_before"])]]}
                snapshot("before-native-eof")
                evidence["eof_input_at"] = tick()
                evidence["eof_exit"] = {"code": 0, "before_forced_cleanup": True}
                evidence["eof_natural"] = copy.deepcopy(evidence["owned"])
                snapshot("native-eof-retired")
            elif interrupted:
                snapshot("before-interrupt")
                evidence["sent"].append({"at": tick(), "message": {"id": 400 + index, "method": "turn/interrupt",
                    "params": {"threadId": "thread", "turnId": turn}}})
                server("cancelled")
                evidence["events"].append({"id": 400 + index, "result": {}})
                completed(turn, "interrupted")
                snapshot("after-interrupt")
            elif no_ask:
                server("response", response={"action": "accept", "content": {}} if case == "data_form_empty"
                       else {"action": "decline"})
                server("refused")
            else:
                if case == "approval_wrong_rpc_id" or case == "approval_stale_after_interrupt":
                    inject("foreign" if case == "approval_wrong_rpc_id" else 200)
                snapshot("before-response")
                result = copy.deepcopy(replies.get(case, {"action": "accept", "content": {"confirmed": True}}))
                evidence["sent"].append({"at": tick(), "message": {
                    "id": index + 200, "result": result}, "synthetic_frontend": True})
                response = copy.deepcopy({key: value for key, value in result.items() if value is not None})
                if case == "approval_missing_content":
                    response["content"] = {}
                server("response", response=response)
                if positive:
                    name = "data" if case == "data_form_never" else "approval"
                    sinks[name].append({**common, "at": tick()})
                    server("appended", sink=f"{name}-sink.jsonl")
                else:
                    server("refused")
        if case != "approval_eof" and not interrupted:
            completed(turn, "completed")
        if case == "approval_duplicate_reply":
            snapshot("before-duplicate")
            inject(200)
    snapshot("before-close")
    evidence["events"].append({"closed": True})
    snapshot("after-close")
    snapshot("after-retirement")
    persist(root, evidence, sinks)
    return evidence, sinks


@pytest.mark.parametrize("case", CASES)
def test_complete_synthetic_raw_witnesses_are_readable(tmp_path, case):
    evidence, _ = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)


def test_missing_content_refuses_after_the_observed_native_empty_object_normalization(tmp_path):
    evidence, sinks = golden(tmp_path, "approval_missing_content")
    evidence["server"][2]["response"] = {"action": "accept", "content": {}}
    persist(tmp_path, evidence, sinks)
    check_case("approval_missing_content", tmp_path, evidence)
    assert not (tmp_path / "approval-sink.jsonl").exists()


@pytest.mark.parametrize("damage", ["absent", "capability", "origin", "persistent"])
def test_missing_frontend_capability_and_user_origin_cannot_qualify(tmp_path, damage):
    evidence, _ = golden(tmp_path, "data_form_never")
    if damage == "absent":
        evidence["sent"] = [row for row in evidence["sent"]
                            if row["message"].get("method") not in {"initialize", "thread/start"}]
    elif damage == "capability":
        evidence["sent"][0]["message"]["params"]["capabilities"].pop("extensions")
    elif damage == "origin":
        evidence["sent"][1]["message"]["params"]["threadSource"] = "subagent"
    else:
        evidence["sent"][1]["message"]["params"]["ephemeral"] = False
    with pytest.raises(ValueError, match="frontend.*profile"):
        check_case("data_form_never", tmp_path, evidence)


@pytest.mark.parametrize("response", [
    {"action": "accept"}, {"action": "accept", "content": None},
    {"action": "accept", "content": {"confirmed": True}},
    {"action": "accept", "content": {}, "_meta": {"persist": "session"}},
])
def test_missing_content_normalization_does_not_allow_other_server_responses(tmp_path, response):
    evidence, sinks = golden(tmp_path, "approval_missing_content")
    evidence["server"][2]["response"] = response
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError, match="negative response"):
        check_case("approval_missing_content", tmp_path, evidence)


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
    reply = next(row for row in evidence["sent"] if row.get("synthetic_frontend"))
    wire = evidence["server"][1]["wire"]
    if damage == "early_write":
        sinks["approval"][0]["at"] = reply["at"] - 1
    elif damage == "late_write":
        sinks["approval"][0]["at"] = evidence["observations"][-1]["at"] + 10
    elif damage == "duplicate":
        sinks["approval"].append(copy.deepcopy(sinks["approval"][0]))
    elif damage == "missing_ask":
        evidence["events"].pop(1)
    elif damage == "wrong_turn":
        ask["params"]["turnId"] = "foreign"
        evidence["observed"][0]["message"] = copy.deepcopy(ask)
    elif damage == "bool_id":
        reply["message"]["id"] = True
    elif damage == "unmarked":
        wire["params"].pop("_meta")
    elif damage == "default_true":
        wire["params"]["requestedSchema"]["properties"]["confirmed"]["default"] = True
    elif damage == "persistence":
        reply["message"]["result"]["_meta"] = {"persist": "session"}
    elif damage == "model_reply":
        reply.pop("synthetic_frontend")
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
        reply["message"]["result"]["content"]["confirmed"] = 1
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
        evidence["observations"][0]["at"] = evidence["observed"][0]["at"] - 1
    else:
        next(row for row in evidence["observations"] if row["label"] == "before-duplicate")["approval"] = None
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("damage", ["pending_missing", "response_before_pending", "raw_missing", "barrier_missing",
                                    "raw_wrongorder", "interrupt_missing", "interrupt_wrongturn", "interrupt_early", "stdout_open"])
def test_lifecycle_observations_are_bound_to_actual_protocol_operations(tmp_path, damage):
    case = "approval_accept_true" if damage == "response_before_pending" else "approval_interrupt" if damage.startswith("interrupt_") else "approval_wrong_rpc_id"
    evidence, sinks = golden(tmp_path, case)
    if damage in {"pending_missing", "raw_missing", "barrier_missing", "interrupt_missing"}:
        label = {"pending_missing": "pending-ask", "raw_missing": "before-negative-injection",
                 "barrier_missing": "after-negative-barrier", "interrupt_missing": "after-interrupt"}[damage]
        evidence["observations"] = [row for row in evidence["observations"] if row["label"] != label]
    elif damage == "response_before_pending":
        next(row for row in evidence["observations"] if row["label"] == "before-response")["at"] = evidence["observed"][0]["at"] - 1
    elif damage == "raw_wrongorder":
        next(row for row in evidence["observations"] if row["label"] == "before-negative-injection")["at"] = next(row["at"] for row in evidence["sent"] if row["message"].get("method") == "config/read") + 1
    elif damage == "interrupt_wrongturn":
        next(row for row in evidence["sent"] if row["message"].get("method") == "turn/interrupt")["message"]["params"]["turnId"] = "foreign"
    elif damage == "interrupt_early":
        next(row for row in evidence["sent"] if row["message"].get("method") == "turn/interrupt")["at"] = evidence["observed"][0]["at"] - 1
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
            upper = next(row["at"] for row in evidence["observations"] if row["label"] == "after-interrupt")
            evidence["server"][-1:] = [
                {**evidence["server"][-1], "event": "response", "response": {"action": "accept", "content": {"confirmed": True}}},
                {**evidence["server"][-1], "event": "refused", "at": upper - 1},
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
        next(row for row in evidence["sent"] if row.get("negative_injection"))["message"]["id"] = 201 if damage == "current_reply" else "foreign"
    elif damage == "early_replay":
        next(row for row in evidence["sent"] if row.get("negative_injection"))["at"] = evidence["observed"][0]["at"] - 1
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


@pytest.mark.parametrize("frame", [{"invalid_json": "late"}, {"id": 99, "error": {}}, [], None])
def test_shutdown_frames_obey_the_active_transport_contract(tmp_path, frame):
    evidence, sinks = golden(tmp_path, "approval_accept_true")
    check_case("approval_accept_true", tmp_path, evidence)
    evidence["events"].insert(-1, frame)
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case("approval_accept_true", tmp_path, evidence)


@pytest.mark.parametrize("case", ["approval_interrupt", "approval_stale_after_interrupt", "approval_eof"])
@pytest.mark.parametrize("kind", ["cancelled", "request_error"])
@pytest.mark.parametrize("phase", ["before_ask", "before_input"])
def test_terminal_before_the_exercised_input_cannot_qualify(tmp_path, case, kind, phase):
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    entry = evidence["server"][0]
    # Damage only the first invocation; its normal instrument was readable.
    evidence["server"] = [row for row in evidence["server"]
                          if row["nonce"] != entry["nonce"] or row["event"] in {"entered", "requested"}]
    at = evidence["observed"][0]["at"] + (-1 if phase == "before_ask" else 1)
    evidence["server"].insert(2, {**{k: entry[k] for k in ("case", "nonce", "incarnation")},
                                 "event": kind, "at": at})
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


def test_frontend_and_server_golden_responses_are_independent(tmp_path):
    evidence, _ = golden(tmp_path, "approval_accept_true")
    frontend = next(row["message"]["result"] for row in evidence["sent"] if row.get("synthetic_frontend"))
    server = next(row["response"] for row in evidence["server"] if row["event"] == "response")
    frontend["content"]["confirmed"] = False
    assert server["content"] == {"confirmed": True}


@pytest.mark.parametrize("damage", ["missing_natural", "survivor", "missing_child", "early_retirement", "dead_writer", "dead_root"])
def test_eof_needs_fresh_live_input_and_natural_child_retirement(tmp_path, damage):
    case = "approval_eof"
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    if damage == "missing_natural":
        evidence.pop("eof_natural", None)
    elif damage == "survivor":
        evidence.setdefault("eof_natural", copy.deepcopy(evidence["owned"]))[0]["after"] = {
            "pid": 20, "start": 20, "state": "S"}
    elif damage == "missing_child":
        evidence["eof_natural"] = []
    elif damage == "early_retirement":
        next(row for row in evidence["observations"] if row["label"] == "native-eof-retired")["at"] = evidence["eof_input_at"] - 1
    else:
        live = evidence.setdefault("eof_live", {"driver": {}, "writers": [{}]})
        (live["driver"] if damage == "dead_root" else live["writers"][0])["state"] = "Z"
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("case", ["approval_wrong_rpc_id", "approval_stale_after_interrupt"])
@pytest.mark.parametrize("damage", ["missing_decline", "cancelled", "wrong_turn", "early_decline"])
def test_foreign_and_stale_controls_require_current_decline_completion(tmp_path, case, damage):
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    current = next(row for row in evidence["sent"] if row.get("synthetic_frontend"))
    if damage == "missing_decline":
        evidence["sent"].remove(current)
    elif damage == "cancelled":
        evidence["server"][-2:] = [{**evidence["server"][-1], "event": "cancelled"}]
    elif damage == "wrong_turn":
        current["message"]["id"] = "foreign"
    else:
        barrier = next(row for row in evidence["observations"] if row["label"] == "after-negative-barrier")
        current["at"] = barrier["at"] - 1
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("case", ["approval_interrupt", "approval_eof"])
@pytest.mark.parametrize("tail", [[], ["cancelled"], ["request_error"], ["response", "refused"], ["response", "cancelled"]])
def test_post_input_terminal_shapes_keep_eof_distinct_from_interruption(tmp_path, case, tail):
    evidence, sinks = golden(tmp_path, case)
    entry = evidence["server"][0]
    if case == "approval_eof":
        lower = evidence["eof_input_at"]
    else:
        lower = next(row["at"] for row in evidence["sent"] if row["message"].get("method") == "turn/interrupt")
    evidence["server"] = evidence["server"][:2] + [{
        **{k: entry[k] for k in ("case", "nonce", "incarnation")}, "at": lower + index + 1, "event": kind,
        **({"response": {"action": "cancel"}} if kind == "response" else {}),
    } for index, kind in enumerate(tail)]
    persist(tmp_path, evidence, sinks)
    if case == "approval_interrupt" and not tail:
        with pytest.raises(ValueError):
            check_case(case, tmp_path, evidence)
    else:
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("case,index", [("approval_accept_true", 0), ("approval_duplicate_reply", 0),
                                       ("approval_two_requests", 0), ("approval_two_requests", 1), ("data_form_never", 0)])
@pytest.mark.parametrize("kind", ["cancelled", "request_error"])
def test_positive_invocations_cannot_hide_an_early_terminal(tmp_path, case, index, kind):
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    entered = [row for row in evidence["server"] if row["event"] == "entered"][index]
    group = [row for row in evidence["server"] if row["nonce"] == entered["nonce"]]
    evidence["server"].insert(evidence["server"].index(group[1]) + 1, {
        **{k: entered[k] for k in ("case", "nonce", "incarnation")}, "event": kind, "at": group[1]["at"] + 1})
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("case,index", [("approval_accept_true", 0), ("approval_decline", 0),
                                       ("approval_wrong_rpc_id", 0), ("approval_stale_after_interrupt", 0),
                                       ("approval_stale_after_interrupt", 1), ("approval_two_requests", 0),
                                       ("approval_two_requests", 1), ("approval_interrupt", 0), ("approval_eof", 0)])
def test_matching_completion_cannot_precede_its_own_ask(tmp_path, case, index):
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    turn = evidence["turns"][index]
    if case == "approval_eof":
        terminal = {"method": "turn/completed", "params": {"threadId": "thread", "turn": {
            "id": turn, "status": "interrupted"}}}
        evidence["completions"].append({"at": evidence["eof_input_at"] + 1, "message": copy.deepcopy(terminal)})
    else:
        terminal = next(row for row in evidence["events"] if row.get("method") == "turn/completed"
                        and row["params"]["turn"]["id"] == turn)
        evidence["events"].remove(terminal)
    ask = next(row for row in evidence["events"] if row.get("method") == "mcpServer/elicitation/request"
               and row["params"]["turnId"] == turn)
    evidence["events"].insert(evidence["events"].index(ask), terminal)
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


def test_eof_has_at_most_one_optional_interrupted_completion(tmp_path):
    evidence, sinks = golden(tmp_path, "approval_eof")
    terminal = {"method": "turn/completed", "params": {"threadId": "thread", "turn": {
        "id": "turn-0", "status": "interrupted"}}}
    evidence["events"].insert(-1, copy.deepcopy(terminal))
    evidence["completions"].append({"at": evidence["eof_input_at"] + 1, "message": copy.deepcopy(terminal)})
    persist(tmp_path, evidence, sinks)
    check_case("approval_eof", tmp_path, evidence)
    evidence["events"].insert(-1, copy.deepcopy(terminal))
    evidence["completions"].append({"at": evidence["eof_input_at"] + 2, "message": copy.deepcopy(terminal)})
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case("approval_eof", tmp_path, evidence)


@pytest.mark.parametrize("case", ["approval_decline", "approval_cancel", "approval_false", "approval_missing_content",
                                 "approval_bad_boolean", "approval_extra_content", "approval_wrong_rpc_id",
                                 "approval_stale_after_interrupt", "approval_never", "data_form_empty", "approval_unattended"])
def test_refusal_must_finish_before_fallback_cleanup(tmp_path, case):
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    close = next(row["at"] for row in evidence["observations"] if row["label"] == "before-close")
    tail = 1 if case == "approval_unattended" else 2
    for index, row in enumerate(evidence["server"][-tail:]):
        row["at"] = close + index + 1
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


def test_duplicate_replay_requires_the_first_effect_already_observed(tmp_path):
    case = "approval_duplicate_reply"
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    barrier = next(row["at"] for row in evidence["observations"] if row["label"] == "after-negative-barrier")
    evidence["server"][2]["at"] = barrier + 1
    sinks["approval"][0]["at"] = barrier + 2
    evidence["server"][3]["at"] = barrier + 3
    raw = "".join(json.dumps(row) + "\n" for row in sinks["approval"]).encode().hex()
    for snapshot in evidence["observations"]:
        snapshot["approval"] = raw if snapshot["at"] > barrier + 2 else None
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("phase", ["before_input", "after_observation"])
def test_completion_receipt_is_bound_to_its_tested_input_and_observation(tmp_path, case, phase):
    evidence, sinks = golden(tmp_path, case)
    if case == "approval_eof":
        message = {"method": "turn/completed", "params": {
            "threadId": "thread", "turn": {"id": "turn-0", "status": "interrupted"}}}
        evidence["events"].insert(-1, copy.deepcopy(message))
        evidence["completions"].append({"at": evidence["eof_input_at"] + 1, "message": message})
        persist(tmp_path, evidence, sinks)
    check_case(case, tmp_path, evidence)
    for index, receipt in enumerate(evidence["completions"]):
        original = receipt["at"]
        status = receipt["message"]["params"]["turn"]["status"]
        if case == "approval_eof":
            lower = evidence["eof_input_at"]
            upper = next(row["at"] for row in evidence["observations"] if row["label"] == "after-close")
        elif status == "interrupted":
            lower = next(row["at"] for row in evidence["sent"] if row["message"].get("method") == "turn/interrupt")
            upper = next(row["at"] for row in evidence["observations"] if row["label"] == "after-interrupt")
        else:
            replies = [row for row in evidence["sent"] if row.get("synthetic_frontend")]
            lower = replies[-1 if case == "approval_stale_after_interrupt" else index]["at"] if replies else evidence["server"][0]["at"]
            upper = next(row["at"] for row in evidence["observations"] if row["label"] == "before-close")
        receipt["at"] = lower - 1 if phase == "before_input" else upper + 1
        persist(tmp_path, evidence, sinks)
        with pytest.raises(ValueError, match="completion receipt"):
            check_case(case, tmp_path, evidence)
        receipt["at"] = original


@pytest.mark.parametrize("damage", ["missing", "extra", "message", "order", "clock_bool", "clock_zero", "clock_text"])
def test_completion_receipts_exactly_match_the_raw_terminal_population(tmp_path, damage):
    evidence, sinks = golden(tmp_path, "approval_two_requests")
    check_case("approval_two_requests", tmp_path, evidence)
    receipts = evidence["completions"]
    if damage == "missing":
        receipts.pop()
    elif damage == "extra":
        receipts.append(copy.deepcopy(receipts[-1]))
    elif damage == "message":
        receipts[0]["message"]["params"]["threadId"] = "foreign"
    elif damage == "order":
        receipts.reverse()
    else:
        receipts[0]["at"] = {"clock_bool": True, "clock_zero": 0, "clock_text": "123"}[damage]
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError):
        check_case("approval_two_requests", tmp_path, evidence)


@pytest.mark.parametrize("case", ["approval_two_requests", "approval_stale_after_interrupt", "approval_duplicate_reply"])
def test_first_completion_receipt_precedes_next_request_or_duplicate_baseline(tmp_path, case):
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    upper = (next(row["at"] for row in evidence["observations"] if row["label"] == "before-duplicate")
             if case == "approval_duplicate_reply" else evidence["observed"][1]["at"])
    evidence["completions"][0]["at"] = upper + 1
    persist(tmp_path, evidence, sinks)
    with pytest.raises(ValueError, match="completion receipt"):
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("damage", ["detached", "missing", "first", "root", "edge", "cycle", "reused", "unknown", None])
def test_fresh_eof_ancestry_checks_edges_and_known_identities_allowing_reparenting(tmp_path, damage):
    evidence, sinks = golden(tmp_path, "approval_eof")
    check_case("approval_eof", tmp_path, evidence)
    intermediate = {"pid": 30, "start": 30, "ppid": 10, "state": "S"}
    owned = {"before": copy.deepcopy(intermediate), "after": {"state": "absent"}, "ancestry": [30, 10]}
    evidence["owned"].append(copy.deepcopy(owned))
    evidence["eof_natural"].append(copy.deepcopy(owned))
    live = evidence["eof_live"]
    # Original writer parent remains10; only fresh parent changes to a known30.
    live["writers"][0]["ppid"] = 30
    live["ancestries"] = [[copy.deepcopy(live["writers"][0]), copy.deepcopy(intermediate), copy.deepcopy(live["driver"])]]
    path = live["ancestries"][0]
    persist(tmp_path, evidence, sinks)
    check_case("approval_eof", tmp_path, evidence)
    if damage == "detached":
        live["writers"][0]["ppid"] = path[0]["ppid"] = 99
    elif damage == "missing":
        live.pop("ancestries")
    elif damage == "first":
        path[0]["start"] += 1
    elif damage == "root":
        path[-1]["start"] += 1
    elif damage == "edge":
        path[1]["ppid"] = 99
    elif damage == "cycle":
        path.insert(1, copy.deepcopy(path[0]))
    elif damage == "reused":
        path[1]["start"] += 1
    elif damage == "unknown":
        path[0]["ppid"] = live["writers"][0]["ppid"] = path[1]["pid"] = 99
    persist(tmp_path, evidence, sinks)
    if damage:
        with pytest.raises(ValueError):
            check_case("approval_eof", tmp_path, evidence)
    else:
        check_case("approval_eof", tmp_path, evidence)


@pytest.mark.parametrize("case", ["approval_interrupt", "approval_stale_after_interrupt", "approval_eof"])
@pytest.mark.parametrize("tail", ["refused", "cancelled"])
@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
def test_retired_response_action_is_checked_inside_a_valid_input_window(tmp_path, case, tail, action):
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    lower = (evidence["eof_input_at"] if case == "approval_eof" else
             next(row["at"] for row in evidence["sent"] if row["message"].get("method") == "turn/interrupt"))
    entry = evidence["server"][0]
    group = [row for row in evidence["server"] if row["nonce"] == entry["nonce"]]
    later = [row for row in evidence["server"] if row["nonce"] != entry["nonce"]]
    evidence["server"] = group[:2] + [
        {**entry, "event": "response", "at": lower + 1, "response": {"action": action}},
        {**entry, "event": tail, "at": lower + 2},
    ] + later
    persist(tmp_path, evidence, sinks)
    if action == "accept":
        with pytest.raises(ValueError, match="interrupted request received an acceptance"):
            check_case(case, tmp_path, evidence)
    else:
        check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("case,index", [
    ("approval_accept_true", 0), ("approval_decline", 0), ("approval_cancel", 0),
    ("approval_false", 0), ("approval_missing_content", 0), ("approval_bad_boolean", 0),
    ("approval_extra_content", 0), ("approval_wrong_rpc_id", 0), ("approval_duplicate_reply", 0),
    ("approval_two_requests", 0), ("approval_two_requests", 1), ("approval_stale_after_interrupt", 1),
    ("approval_never", 0), ("approval_unattended", 0), ("data_form_never", 0), ("data_form_empty", 0),
])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_normal_completion_follows_its_own_fixture_outcome(tmp_path, case, index, offset):
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    entry = [row for row in evidence["server"] if row["event"] == "entered"][index]
    terminal = [row for row in evidence["server"] if row["nonce"] == entry["nonce"]][-1]
    evidence["completions"][index]["at"] = terminal["at"] + offset
    persist(tmp_path, evidence, sinks)
    if offset > 0:
        check_case(case, tmp_path, evidence)
    else:
        with pytest.raises(ValueError, match="completion receipt"):
            check_case(case, tmp_path, evidence)


@pytest.mark.parametrize("case", ["approval_interrupt", "approval_stale_after_interrupt"])
@pytest.mark.parametrize("damage", [None, "string_id", "missing", "late", "error", "notification",
                                   "no_result", "wrong_result", "wrong_type", "bool_alias", "duplicate"])
def test_interrupt_completion_follows_its_successful_typed_rpc_ack(tmp_path, case, damage):
    evidence, sinks = golden(tmp_path, case)
    check_case(case, tmp_path, evidence)
    interrupt = next(row["message"] for row in evidence["sent"] if row["message"].get("method") == "turn/interrupt")
    ack = next(row for row in evidence["events"] if row.get("id") == interrupt["id"])
    if damage == "missing":
        evidence["events"].remove(ack)
    elif damage == "late":
        evidence["events"].remove(ack)
        position = next(i for i, row in enumerate(evidence["events"]) if row.get("method") == "turn/completed")
        evidence["events"].insert(position + 1, ack)
    elif damage == "error":
        ack.pop("result")
        ack["error"] = {"code": -32000, "message": "failed interrupt"}
    elif damage == "notification":
        ack["method"] = "unexpected"
    elif damage == "no_result":
        ack.pop("result")
    elif damage == "wrong_result":
        ack["result"] = None
    elif damage == "wrong_type":
        ack["id"] = str(interrupt["id"])
    elif damage == "bool_alias":
        interrupt["id"], ack["id"] = 1, True
    elif damage == "duplicate":
        evidence["events"].insert(evidence["events"].index(ack), copy.deepcopy(ack))
    elif damage == "string_id":
        interrupt["id"] = ack["id"] = "interrupt-400"
    persist(tmp_path, evidence, sinks)
    if damage in (None, "string_id"):
        check_case(case, tmp_path, evidence)
    else:
        with pytest.raises(ValueError):
            check_case(case, tmp_path, evidence)
