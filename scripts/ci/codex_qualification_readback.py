"""Independent, credential-free native fixture readback; never production readiness."""

import argparse
import hashlib
import json
from pathlib import Path

from genesis.eval.qualification.evidence import digest, load_json

APPROVAL_DECISIONS = ("accept", "decline", "interrupt", "stale", "disconnect", "steer")
ASK_MODES = ("safe", "raw-ask", "adapted-ask")
CLIENTS = ("cli", "app")
SOURCE_PATHS = ("src", "scripts", "tests", ".claude/hooks", ".github", "pyproject.toml")
ACCEPTED_STEER = "fixture-accepted-steer"
REJECTED_STEER = "fixture-rejected-steer"
ADAPTER_ALLOW_CASES = ("shell", "patch", "code_shell", "code_patch", "mcp", "mcp_direct", "advisory", "allow-reason", "patch-relative", "outer-timeout")


def required_cases():
    prefix = "tests/test_hooks/test_codex_compatibility_native.py::"
    # Frozen inherited population, independently checked against pytest collection.
    # A change to those suites updates this census deliberately, never by dropping cells.
    allow = ADAPTER_ALLOW_CASES
    deny = ("deny", "ask", "crash", "missing", "malformed", "timeout", "patch-deny", "missing-launcher", "broken-launcher", "silent-launcher", "mcp_direct-deny", "mcp-deny", "outer-completes", "legacy-anchor")
    hooks = {f"tests/test_hooks/test_codex_native.py::test_native_hook_controls_actual_sink[{decision}-{action}-{client}]"
             for decision in ("allow", "deny") for action in ("shell", "patch", "mcp", "code_shell", "code_patch") for client in CLIENTS}
    adapters = {f"tests/test_hooks/test_codex_validator_adapter_native.py::test_native_adapter_controls_sink[{case}-{instrument}-{client}]"
                for instrument, cases in (("adapter", allow + deny), ("noop", deny)) for case in cases for client in CLIENTS}
    return (hooks | adapters | {f"{prefix}test_native_approval[{decision}]" for decision in APPROVAL_DECISIONS}
            | {f"{prefix}test_canonical_ask_route[{mode}-{client}]" for mode in ASK_MODES for client in CLIENTS}
            | {f"{prefix}test_native_resume", f"{prefix}test_required_mcp_and_owned_cleanup[provenance-cleanup]",
               f"{prefix}test_required_mcp_and_owned_cleanup[required-failure]"})

def inventory(root, paths):
    found = {}
    for relative in paths:
        path = root / relative
        for entry in sorted(path.rglob("*") if path.is_dir() else [path]):
            if entry.is_file() and "__pycache__" not in entry.parts and entry.suffix != ".pyc":
                found[str(entry.relative_to(root))] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return found


def readback(manifest, report, artifacts, *, head, base):
    if manifest.get("schema") != "codex-compatibility/v1" or manifest.get("head") != head or manifest.get("base") != base:
        raise ValueError("stale qualification identity")
    if report.get("manifest_digest") != digest(manifest):
        raise ValueError("report does not bind the expected source/native manifest")
    cases = manifest["cases"]
    if set(cases) != required_cases() or len(set(cases)) != len(cases) or set(report["cases"]) != set(cases):
        raise ValueError("incomplete case census")
    if type(report.get("exitstatus")) is not int or report["exitstatus"] != 0:
        raise ValueError("failed qualification run")
    if report["artifacts"] != inventory(artifacts, (".",)):
        raise ValueError("missing or changed native artifacts")
    receipts = set()
    for nodeid in cases:
        row = report["cases"][nodeid]
        if row["phases"] != [[phase, "passed"] for phase in ("setup", "call", "teardown")]:
            raise ValueError("partial, skipped or failed test phases")
        receipt = row.get("receipt", "")
        path = Path(receipt)
        if not receipt or path.is_absolute() or ".." in path.parts or receipt in receipts or receipt not in report["artifacts"]:
            raise ValueError("missing or duplicated native receipt")
        receipts.add(receipt)
        evidence = load_json((artifacts / receipt).read_bytes())
        if not isinstance(evidence, dict):
            raise ValueError("native receipt is not an object")
        clients = [client for client in CLIENTS if nodeid.endswith(f"-{client}]")]
        if clients and evidence.get("client") != clients[0]:
            raise ValueError("native receipt belongs to another client")
        if "test_codex_compatibility_native.py::" in nodeid:
            root = (artifacts / receipt).parent
            expected_files = {name: value for name, value in inventory(root, (".",)).items()
                              if name != "compat-evidence.json"}
            if evidence.get("files") != expected_files or evidence.get("fixture_consent") is not True or evidence.get("production_ready") is not False:
                raise ValueError("native receipt lost its raw witnesses")
            try:
                check_native_receipt(nodeid, root, evidence)
            except (KeyError, TypeError, StopIteration) as exc:
                raise ValueError("missing native witness") from exc
        elif (evidence.get("versions") != {"cli": "codex-cli 0.166.0", "app": "codex-cli 0.166.0"}
              or len(evidence.get("requests", [])) != 2 or not completed_route(evidence.get("events", []), evidence.get("client"))):
            raise ValueError("missing inherited native transport witnesses")
        else:
            check_inherited_receipt(nodeid, (artifacts / receipt).parent, evidence)
    # Results apply only to this transport/guard route. Same-session canonical
    # approval remains unproven; no adapter, auth or production path was built.
    return {"status": "complete", "case_total": len(cases), "production_ready": False,
            "native_command_approval": "supported", "canonical_ask_native_wire": "unsupported",
            "canonical_same_session_approval": "unproven", "head": head, "base": base}


def check_inherited_receipt(nodeid, root, evidence):
    params = nodeid.rsplit("[", 1)[1][:-1]
    if "test_codex_native.py::" in nodeid:
        decision, action, _ = params.split("-")
        if evidence.get("decision") != decision or evidence.get("action") != action:
            raise ValueError("inherited receipt belongs to another hook case")
        sink = root / f"workspace/{action.removeprefix('code_')}-receipt.txt"
        allowed = decision == "allow"
    else:
        case, instrument, _ = params.rsplit("-", 2)
        if evidence.get("case") != case or evidence.get("instrument") != instrument:
            raise ValueError("inherited receipt belongs to another adapter case")
        kind = "patch" if "patch" in case else "mcp" if case.startswith("mcp") else "shell"
        directory = "tmp/validator/session/workspace" if case == "patch-relative" else "workspace" if case == "patch-deny" or kind == "mcp" else "tmp/validator"
        sink = root / directory / f"{kind}-receipt.txt"
        # Raw direct MCP calls never reach a hook, including no-op controls.
        allowed = not case.startswith("mcp_direct") and (instrument == "noop" or case in ADAPTER_ALLOW_CASES)
    if sink.exists() != allowed or (allowed and sink.read_bytes().strip() != b"fixture"):
        raise ValueError("inherited native effect contradicts its case")


def check_sink(path, evidence, allowed):
    if evidence.get("sink") is not allowed or path.exists() != allowed or (allowed and path.read_bytes() != b"fixture"):
        raise ValueError("native effect contradicts expected decision")


def terminal(evidence, status="completed"):
    thread, turn = evidence["thread"], evidence["turn"]
    if not isinstance(thread, str) or not thread or not isinstance(turn, str) or not turn:
        raise ValueError("missing native session identity")
    frames = [event["params"] for event in evidence["events"] if event.get("method") == "turn/completed"]
    statuses = [frame["turn"].get("status") for frame in frames
                if frame.get("threadId") == thread and frame.get("turn", {}).get("id") == turn]
    if statuses != [status]:
        raise ValueError("missing correlated native terminal event")


def completed_route(events, client):
    if client == "cli":
        return [event.get("type") for event in events if event.get("type") in {"turn.completed", "turn.failed"}] == ["turn.completed"]
    if client == "app":
        return [event["params"]["turn"]["status"] for event in events if event.get("method") == "turn/completed"] == ["completed"]
    return False


def check_native_receipt(nodeid, root, evidence):
    events, requests = evidence["events"], evidence["model_requests"]
    if not isinstance(events, list) or not events or not isinstance(requests, list):
        raise ValueError("missing native events or provider requests")
    if "::test_native_approval[" in nodeid:
        decision = nodeid.rsplit("[", 1)[1][:-1]
        starts = [event["result"] for event in events if event.get("id") == 2 and "method" not in event and "result" in event]
        if len(starts) != 1 or starts[0].get("thread", {}).get("id") != evidence["thread"] or starts[0].get("approvalPolicy") != "on-request" or starts[0].get("sandbox", {}).get("type") != "readOnly":
            raise ValueError("missing native approval policy witness")
        check_sink(root / "workspace/approval.txt", evidence, decision == "accept")
        approvals = [event["params"] for event in events if event.get("method") == "item/commandExecution/requestApproval"]
        if evidence["decision"] != decision or not requests or not any(
            row["threadId"] == evidence["thread"] and row["turnId"] == evidence["turn"] and "approval.txt" in row["command"]
            for row in approvals
        ):
            raise ValueError("missing correlated native approval request")
        if decision == "disconnect":
            if evidence["eof_exit"] != {"code": 0, "before_forced_cleanup": True}:
                raise ValueError("missing native EOF shutdown witness")
        else:
            terminal(evidence, "interrupted" if decision in {"interrupt", "stale"} else "completed")
        if decision == "steer" and (evidence["steer"]["turnId"] != evidence["turn"] or
                ACCEPTED_STEER in json.dumps(requests[0]) or REJECTED_STEER in json.dumps(requests) or
                not any(ACCEPTED_STEER in json.dumps(row) for row in requests[1:])):
            raise ValueError("steering input was not delivered to the active turn")
    elif "::test_canonical_ask_route[" in nodeid:
        mode = next(mode for mode in ASK_MODES if any(nodeid.endswith(f"[{mode}-{client}]") for client in CLIENTS))
        check_sink(root / "workspace/canonical-receipt.txt", evidence, mode != "adapted-ask")
        guard = load_json((root / "guard-result.json").read_bytes())
        if guard["code"] != 0 or (mode == "safe" and guard["stdout"] != "") or (mode != "safe" and load_json(guard["stdout"])["hookSpecificOutput"]["permissionDecision"] != "ask"):
            raise ValueError("missing actual canonical ask witness")
        if len(requests) != 2 or not completed_route(events, evidence["client"]):
            raise ValueError("missing canonical route native completion")
    elif nodeid.endswith("::test_native_resume"):
        terminal(evidence)
        if evidence["resume"]["thread"]["id"] != evidence["thread"] or len(requests) != 2 or (root / "workspace/resume.txt").read_bytes() != b"fixture":
            raise ValueError("native resume lost identity, action or no-replay witness")
        if not any(event.get("result") == evidence["resume"] for event in evidence["resume_events"]):
            raise ValueError("missing native resume response")
    elif nodeid.endswith("[required-failure]"):
        response = evidence["failure_response"]
        message = response.get("error", {}).get("message", "").lower()
        if requests or response not in events or response.get("id") != 2 or "mcp" not in message or "fixture" not in message:
            raise ValueError("required MCP failure did not precede model work")
    elif nodeid.endswith("[provenance-cleanup]"):
        terminal(evidence)
        before, after, ancestry = evidence["child_before"], evidence["child_after"], evidence["owned_ancestry"]
        if not requests or evidence["cleanup_confirmed"] is not True or (root / "marker").read_text() != "1":
            raise ValueError("missing provenance/owned cleanup witnesses")
        if type(before["pid"]) is not int or before["pid"] <= 1 or type(before["start"]) is not int or before["start"] <= 0 or before["state"] == "Z":
            raise ValueError("descendant was not observed ready and alive")
        if len(ancestry) < 2 or ancestry[0] != before["pid"] or ancestry[-1] != evidence["driver_pid"]:
            raise ValueError("descendant ownership was not observed")
        if after["state"] != "absent":
            if type(after.get("start")) is not int or after["start"] <= 0:
                raise ValueError("missing post-cleanup identity")
            if after["state"] != "Z" and after["start"] == before["start"]:
                raise ValueError("owned descendant survived native cleanup")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("artifacts", type=Path)
    parser.add_argument("--head", required=True)
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    manifest = load_json(args.manifest.read_bytes())
    if manifest["sources"] != inventory(Path(__file__).resolve().parents[2], SOURCE_PATHS):
        raise ValueError("source snapshot no longer matches qualification")
    result = readback(manifest, load_json(args.report.read_bytes()), args.artifacts,
                      head=args.head, base=args.base)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
