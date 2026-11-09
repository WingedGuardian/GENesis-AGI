"""Independent, closed-census readback for the harmless MCP approval experiment."""

import argparse
import json
import re
from pathlib import Path

from genesis.eval.qualification.evidence import canonical, digest, load_json
from scripts.ci.codex_qualification_readback import SOURCE_PATHS, inventory

CASES = (
    "approval_accept_true", "approval_decline", "approval_cancel", "approval_false",
    "approval_missing_content", "approval_bad_boolean", "approval_extra_content",
    "approval_wrong_rpc_id", "approval_duplicate_reply", "approval_interrupt",
    "approval_eof", "approval_two_requests", "approval_stale_after_interrupt",
    "approval_never", "approval_unattended", "data_form_never", "data_form_empty",
)
PREFIX = "tests/test_hooks/test_codex_elicitation_native.py::test_native_elicitation"
INVENTORY = (*SOURCE_PATHS, "python-packages.txt", "os-packages.txt")
META = {"codex_approval_kind": "mcp_tool_call", "codex_request_type": "approval_request",
        "codex_requires_user_input": True, "tool_name": "record",
        "tool_params": {"target": "approval-sink.jsonl"}}
NO_ASK = {"approval_never", "approval_unattended", "data_form_empty"}
POSITIVE = {"approval_accept_true", "approval_duplicate_reply", "approval_two_requests"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def same(left, right):
    # JSON equality must distinguish true from 1 (and RPC integer from string).
    return canonical(left) == canonical(right)


def identity(value):
    require(type(value) in (int, str), "invalid native request identity")
    return type(value), value


def instant(value):
    require(type(value) is int and value > 0, "missing monotonic ordering witness")
    return value


def lines(path):
    if not path.exists():
        return []
    # Decode physical JSONL records with the shared strict decoder. The older
    # Headlong read_jsonl uses permissive json.loads, which is not equivalent.
    with path.open("rb") as stream:
        return [load_json(line) for line in stream]


def turn_statuses(case):
    if case == "approval_stale_after_interrupt":
        return ("interrupted", "completed")
    if case == "approval_interrupt":
        return ("interrupted",)
    return ("completed",) * (2 if case == "approval_two_requests" else 1)


def reply_slots(case):
    return [] if case in NO_ASK or case == "approval_eof" else [
        index for index, status in enumerate(turn_statuses(case)) if status == "completed"]


def response_for(case):
    if case in {"approval_decline", "approval_cancel", "approval_wrong_rpc_id", "approval_stale_after_interrupt"}:
        return {"action": "cancel" if case == "approval_cancel" else "decline", "content": None}
    result = {"action": "accept", "content": {"confirmed": True}}
    if case == "approval_false":
        result["content"]["confirmed"] = False
    elif case == "approval_bad_boolean":
        result["content"]["confirmed"] = "true"
    elif case == "approval_extra_content":
        result["content"]["extra"] = True
    elif case == "approval_missing_content":
        result.pop("content")
    elif case == "data_form_never":
        result["content"] = {"value": True}
    return result


def check_ownership(evidence, entered):
    driver = evidence["driver_before"]
    pid = evidence["driver_pid"]
    require(type(pid) is int and pid > 1 and driver["pid"] == pid
            and driver["pgid"] == pid and driver["sid"] == pid, "unowned native driver")
    instant(driver["start"])
    require(driver["state"] != "Z", "native driver was not alive")
    observed = set()
    for item in evidence["owned"]:
        before, after, ancestry = item["before"], item["after"], item["ancestry"]
        instant(before["start"])
        require(type(before["pid"]) is int and before["pid"] > 1 and before["state"] != "Z"
                and len(ancestry) >= 2 and ancestry[0] == before["pid"]
                and ancestry[1] == before["ppid"] and ancestry[-1] == pid
                and len(set(ancestry)) == len(ancestry), "missing live descendant ownership")
        key = (before["pid"], before["start"])
        require(key not in observed, "duplicate owned process witness")
        observed.add(key)
        if after["state"] != "absent":
            instant(after["start"])
            require(after["pid"] == before["pid"] and
                    (after["state"] == "Z" or after["start"] != before["start"]),
                    "owned child survived cleanup")
    require(observed and all((row["process"]["pid"], row["process"]["start"]) in observed
                             for row in entered), "missing MCP process retirement")
    require(type(evidence["exit_code"]) is int and evidence["exit_code"] in (0, -15),
            "native close required forced kill or failed")


def check_natural_eof(evidence, entered):
    live = evidence["eof_live"]
    driver = live["driver"]
    original = evidence["driver_before"]
    require(all(driver[key] == original[key] for key in ("pid", "start", "pgid", "sid"))
            and driver["state"] in {"R", "S", "D", "T", "t", "K", "W", "P", "I"},
            "native root was not live immediately before EOF")
    writers = live["writers"]
    require(len(writers) == len(entered) and all(
        row["pid"] == entry["process"]["pid"] and row["start"] == entry["process"]["start"]
        and row["state"] in {"R", "S", "D", "T", "t", "K", "W", "P", "I"}
        for row, entry in zip(writers, entered, strict=True)),
        "active MCP writer was not live immediately before EOF")
    natural = evidence["eof_natural"]
    require(same([{k: v for k, v in row.items() if k != "after"} for row in natural],
                 [{k: v for k, v in row.items() if k != "after"} for row in evidence["owned"]]),
            "natural EOF retirement omitted or substituted an owned child")
    check_ownership({**evidence, "owned": natural}, entered)
    labels = {row["label"]: row["at"] for row in evidence["observations"]}
    require(instant(evidence["observed"][-1]["at"]) < instant(live["at"])
            < instant(labels["before-native-eof"]) < instant(evidence["eof_input_at"])
            < instant(labels["native-eof-retired"]) < instant(labels["before-close"]),
            "natural EOF proof was not captured before fallback cleanup")


def check_schema(params, case):
    require(params["mode"] == "form", "wrong elicitation mode")
    schema = params["requestedSchema"]
    require(schema["type"] == "object", "wrong form schema")
    if case == "data_form_empty":
        require(schema["properties"] == {} and params.get("_meta") in (None, {}),
                "empty data control became an approval request")
    else:
        field = "value" if case == "data_form_never" else "confirmed"
        require(set(schema["properties"]) == {field} and schema["required"] == [field]
                and schema["properties"][field]["type"] == "boolean"
                and "default" not in schema["properties"][field], "boolean form lost its consent constraint")
        require(params.get("_meta") in (None, {}) if field == "value" else same(params["_meta"], META),
                "proper approval metadata was lost or fabricated")


def _check_case(case, root, evidence):
    require(case in CASES and evidence["schema"] == "codex-elicitation/v1"
            and evidence["case"] == case and evidence["fixture_consent"] is True
            and evidence["production_ready"] is False, "wrong fixture identity or authority claim")
    files = {name: value for name, value in inventory(root, (".",)).items()
             if name != "elicitation-evidence.json"}
    require(evidence["files"] == files, "case artifact drift")
    require(same(evidence["server"], lines(root / "server.jsonl")), "server log drift")
    check_native_run(case, evidence)
    check_frontend(case, evidence)
    check_observation_order(case, evidence)
    check_effects(case, root, evidence)
    check_server(case, root, evidence)


def check_native_stream(events):
    require(events and all(isinstance(row, dict) and "invalid_json" not in row and "error" not in row
                           for row in events), "malformed or failed retained native stream")
    require(events[-1] == {"closed": True} and not any("closed" in row for row in events[:-1]),
            "native stdout did not reach EOF")


def check_completions(case, evidence):
    events, thread, turns = evidence["events"], evidence["thread"], evidence["turns"]
    terminal = [event["params"] for event in events if event.get("method") == "turn/completed"]
    if case == "approval_eof":
        require(same(evidence["eof_exit"], {"code": 0, "before_forced_cleanup": True})
                and evidence["exit_code"] == 0, "EOF was not observed before fallback cleanup")
        require(len(terminal) <= 1 and all(row["threadId"] == thread and row["turn"]["id"] in turns
                    and row["turn"]["status"] == "interrupted" for row in terminal), "unexpected EOF terminal")
    else:
        require([(row["threadId"], row["turn"]["id"], row["turn"]["status"]) for row in terminal]
                == [(thread, turn, status) for turn, status in zip(turns, turn_statuses(case), strict=True)], "missing correlated native completion")
    for position, event in enumerate(events):
        if event.get("method") != "turn/completed" or case in NO_ASK:
            continue
        ask_positions = [index for index, row in enumerate(events)
                         if row.get("method") == "mcpServer/elicitation/request"
                         and row["params"]["turnId"] == event["params"]["turn"]["id"]]
        require(len(ask_positions) == 1 and ask_positions[0] < position,
                "native completion preceded its own request")


def check_native_run(case, evidence):
    events = evidence["events"]
    check_native_stream(events)
    initialization = [row["message"] for row in evidence["sent"]
                      if row["message"].get("method") == "initialize"]
    starts = [row["message"] for row in evidence["sent"]
              if row["message"].get("method") == "thread/start"]
    require(len(initialization) == len(starts) == 1
            and same(initialization[0]["params"]["capabilities"], {
                "experimentalApi": True, "extensions": {"openai/standard-form-input": {}}})
            and starts[0]["params"]["threadSource"] == "user"
            and starts[0]["params"]["ephemeral"] is True, "missing frontend form-input profile")
    thread, turns = evidence["thread"], evidence["turns"]
    count = 2 if case in {"approval_two_requests", "approval_stale_after_interrupt"} else 1
    require(isinstance(thread, str) and thread and len(turns) == len(set(turns)) == count
            and all(isinstance(turn, str) and turn for turn in turns), "missing native thread/turn identity")
    never = case in {"approval_never", "data_form_never", "data_form_empty"}
    started = evidence["started"]
    require(started["thread"]["id"] == thread and started["approvalPolicy"] == ("never" if never else "on-request")
            and started["sandbox"]["type"] == ("dangerFullAccess" if never else "readOnly")
            and any(same(event.get("result"), started) and event.get("id") == 2 for event in events),
            "missing actual native policy witness")
    config = evidence["trust"]["effective_config"]
    require(config["approvals_reviewer"] == "user" and config["mcp_servers"]["fixture"]["default_tools_approval_mode"] == "approve",
            "an alternate reviewer or ordinary tool prompt confounded the experiment")
    require(len(evidence["model_requests"]) == (1 if case == "approval_eof" else sum(1 if status == "interrupted" else 2 for status in turn_statuses(case))),
            "unexpected provider action population")
    check_completions(case, evidence)
    entered = [row for row in evidence["server"] if row["event"] == "entered"]
    require(len(entered) == count and len({row["nonce"] for row in entered}) == count,
            "missing or reused server invocation")
    check_ownership(evidence, entered)
    if case == "approval_eof":
        check_natural_eof(evidence, entered)


def check_frontend(case, evidence):
    events, sent, observed = evidence["events"], evidence["sent"], evidence["observed"]
    count = 2 if case in {"approval_two_requests", "approval_stale_after_interrupt"} else 1
    asks = [event for event in events if event.get("method") == "mcpServer/elicitation/request"]
    require(len(asks) == (0 if case in NO_ASK else count) and len(observed) == len(asks)
            and all(same(row["message"], ask) for row, ask in zip(observed, asks, strict=True)),
            "native request census disagrees with observed frontend")
    require(len({identity(ask["id"]) for ask in asks}) == len(asks), "native request id reused")
    require(not any("id" in event and "method" in event and event["method"] != "mcpServer/elicitation/request"
                    for event in events), "unexpected native approval route")
    replies = [row for row in sent if "result" in row["message"]]
    normal = [row for row in replies if row.get("synthetic_frontend") is True and "negative_injection" not in row]
    raw = [row for row in replies if row.get("negative_injection") is True and "synthetic_frontend" not in row]
    expected_normal = len(reply_slots(case))
    require(len(normal) == expected_normal and len(raw) == int(case in {
        "approval_wrong_rpc_id", "approval_duplicate_reply", "approval_stale_after_interrupt"})
        and len(replies) == len(normal) + len(raw), "reply provenance or population drift")
    for index, reply in zip(reply_slots(case), normal, strict=True):
        require(identity(reply["message"]["id"]) == identity(asks[index]["id"])
                and same(reply["message"]["result"], response_for(case)), "foreign or malformed frontend reply")
        require(instant(observed[index]["at"]) < instant(reply["at"]), "reply preceded observed request")
    if raw:
        key = identity(raw[0]["message"]["id"])
        require(same(raw[0]["message"]["result"], {"action": "accept", "content": {"confirmed": True}}),
                "negative injection lost its attempted acceptance")
        require(key not in {identity(ask["id"]) for ask in asks} if case == "approval_wrong_rpc_id"
                else key == identity(asks[0]["id"]), "wrong replay identity")
        barrier = [row for row in sent if row["message"].get("method") == "config/read"]
        require(len(barrier) == 1 and instant(raw[0]["at"]) < instant(barrier[0]["at"])
                and any(identity(event["id"]) == identity(barrier[0]["message"]["id"])
                        and "result" in event for event in events if "id" in event), "missing ordered negative barrier")
        if case == "approval_duplicate_reply":
            require(raw[0]["at"] > normal[0]["at"], "replay did not follow consumed reply")
        if case == "approval_stale_after_interrupt":
            require(raw[0]["at"] > observed[1]["at"], "stale reply did not target a later pending request")


def check_effects(case, root, evidence):
    events, sent = evidence["events"], evidence["sent"]
    count = 2 if case in {"approval_two_requests", "approval_stale_after_interrupt"} else 1
    asks = [event for event in events if event.get("method") == "mcpServer/elicitation/request"]
    normal = [row for row in sent if "result" in row["message"] and row.get("synthetic_frontend") is True]
    entered = [row for row in evidence["server"] if row["event"] == "entered"]
    snapshots = evidence["observations"]
    require(all(instant(a["at"]) < instant(b["at"]) for a, b in zip(snapshots, snapshots[1:], strict=False)), "unordered sink observations")
    require([row["label"] for row in snapshots[-3:]] == ["before-close", "after-close", "after-retirement"], "missing close/retirement observations")
    require([row["label"] for row in snapshots].count("pending-ask") == len(asks)
            and [row["label"] for row in snapshots].count("before-response") == len(normal), "missing pre-response sink barrier")
    for name in ("approval", "data"):
        sink = root / f"{name}-sink.jsonl"
        actual = sink.read_bytes().hex() if sink.exists() else None
        require(all(row[name] == actual for row in snapshots[-2:]), "sink changed during shutdown")
        expected = (count if case in POSITIVE else 0) if name == "approval" else int(case == "data_form_never")
        sink_rows = lines(sink)
        require(all(instant(a["at"]) < instant(b["at"]) for a, b in zip(sink_rows, sink_rows[1:], strict=False)),
                "sink is not an ordered append-only stream")
        require(len(sink_rows) == expected and sink.exists() is bool(expected), "unexpected physical sink effect")
        require(len({row["nonce"] for row in sink_rows}) == expected, "duplicate sink append")
        for row in sink_rows:
            index = next(i for i, entry in enumerate(entered) if entry["nonce"] == row["nonce"])
            require(row["case"] == case and row["incarnation"] == entered[index]["incarnation"]
                    and instant(normal[index]["at"]) < instant(row["at"]), "sink write preceded its own consent")
        physical = sink.read_bytes().splitlines(keepends=True) if sink.exists() else []
        for snapshot in snapshots:
            prior = [row for row in sink_rows if instant(row["at"]) < instant(snapshot["at"])]
            prefix = b"".join(physical[:len(prior)]).hex() if prior else None
            require(snapshot[name] == prefix, "sink snapshot contradicts its chronological append-only prefix")
        for index, snapshot in enumerate(row for row in snapshots if row["label"] == "pending-ask"):
            baseline = index if name == "approval" and case in POSITIVE else 0
            require(len(bytes.fromhex(snapshot[name]).splitlines()) == baseline if snapshot[name] is not None
                    else baseline == 0, "pending ask already contains an unauthorized effect")


def check_observation_order(case, evidence):
    snapshots, sent, observed = evidence["observations"], evidence["sent"], evidence["observed"]
    labels = {}
    for row in snapshots:
        labels.setdefault(row["label"], []).append(instant(row["at"]))
    normal = [row for row in sent if row.get("synthetic_frontend") is True]
    raw = [row for row in sent if row.get("negative_injection") is True]
    interrupted_slots = [index for index, status in enumerate(turn_statuses(case)) if status == "interrupted"]
    interrupted = len(interrupted_slots)
    counts = {"pending-ask": len(observed), "before-response": len(normal),
              "before-negative-injection": len(raw), "after-negative-barrier": len(raw),
              "before-interrupt": interrupted, "after-interrupt": interrupted,
              "before-duplicate": int(case == "approval_duplicate_reply"),
              "before-native-eof": int(case == "approval_eof"), "native-eof-retired": int(case == "approval_eof"),
              "before-close": 1, "after-close": 1, "after-retirement": 1}
    require({label: len(rows) for label, rows in labels.items()} == {k: v for k, v in counts.items() if v},
            "missing, duplicate or unknown sink observation")
    for index, row in enumerate(observed):
        require(row["at"] < labels["pending-ask"][index] < labels["before-close"][0], "unbound pending snapshot")
    for position, (index, row) in enumerate(zip(reply_slots(case), normal, strict=True)):
        require(labels["pending-ask"][index] < labels["before-response"][position]
                < row["at"] < labels["before-close"][0], "unbound response snapshot")
    if raw:
        barrier = next(row for row in sent if row["message"].get("method") == "config/read")
        require(labels["pending-ask"][-1] < labels["before-negative-injection"][0] < raw[0]["at"]
                < barrier["at"] < labels["after-negative-barrier"][0] < labels["before-close"][0],
                "unbound negative injection/barrier snapshots")
    if case == "approval_duplicate_reply":
        require(normal[0]["at"] < labels["before-duplicate"][0] < labels["before-negative-injection"][0],
                "duplicate baseline was not observed before replay")
    interrupts = [row for row in sent if row["message"].get("method") == "turn/interrupt"]
    require(len(interrupts) == interrupted, "missing native interruption input")
    for position, (index, row) in enumerate(zip(interrupted_slots, interrupts, strict=True)):
        params = row["message"]["params"]
        require(params["threadId"] == evidence["thread"] and params["turnId"] == evidence["turns"][index]
                and labels["pending-ask"][index] < labels["before-interrupt"][position] < row["at"]
                < labels["after-interrupt"][position] < labels["before-close"][0], "uncorrelated interruption input")
    if case == "approval_stale_after_interrupt":
        require(labels["after-interrupt"][0] < observed[1]["at"]
                and labels["after-negative-barrier"][0] < labels["before-response"][0], "stale replay did not precede current decline")
    elif case == "approval_wrong_rpc_id":
        require(labels["after-negative-barrier"][0] < labels["before-response"][0], "foreign reply did not precede current decline")


def check_retired_request(case, evidence, group, index):
    sent, turns = evidence["sent"], evidence["turns"]
    tail = [row["event"] for row in group[2:]]
    require(tail in (([], ["cancelled"], ["request_error"], ["response", "refused"], ["response", "cancelled"])
                     if case == "approval_eof" else (["cancelled"], ["request_error"], ["response", "refused"], ["response", "cancelled"])),
            "interrupted request produced an unexplained effect")
    if tail:
        if case == "approval_eof":
            lower = instant(evidence["eof_input_at"])
            upper = next(row["at"] for row in evidence["observations"] if row["label"] == "native-eof-retired")
        else:
            lower = next(row["at"] for row in sent if row["message"].get("method") == "turn/interrupt"
                         and row["message"]["params"]["turnId"] == turns[index])
            position = [i for i, status in enumerate(turn_statuses(case)) if status == "interrupted"].index(index)
            upper = [row["at"] for row in evidence["observations"] if row["label"] == "after-interrupt"][position]
        require(lower < group[2]["at"] <= group[-1]["at"] < upper,
                "terminal was not after its tested input and before cleanup")
    if tail and tail[0] == "response":
        require(group[2]["response"].get("action") in {"cancel", "decline"},
                "interrupted request received an acceptance")


def check_server(case, root, evidence):
    events, sent, observed = evidence["events"], evidence["sent"], evidence["observed"]
    thread, turns = evidence["thread"], evidence["turns"]
    asks = [event for event in events if event.get("method") == "mcpServer/elicitation/request"]
    normal = [row for row in sent if "result" in row["message"] and row.get("synthetic_frontend") is True]
    entered = [row for row in evidence["server"] if row["event"] == "entered"]
    before_close = next(row["at"] for row in evidence["observations"] if row["label"] == "before-close")
    for index, entry in enumerate(entered):
        require(entry["case"] == case and re.fullmatch(r"[0-9a-f]{32}", entry["nonce"])
                and re.fullmatch(r"[0-9a-f]{32}", entry["incarnation"])
                and entry["protocol"] == "2025-06-18" and isinstance(entry["capabilities"]["elicitation"], dict)
                and entry["unattended"] is (case == "approval_unattended"), "wrong protocol/capability/role witness")
        group = [row for row in evidence["server"] if row["nonce"] == entry["nonce"]]
        require(group[0] == entry and all(row["case"] == case and row["incarnation"] == entry["incarnation"] for row in group)
                and all(instant(a["at"]) < instant(b["at"]) for a, b in zip(group, group[1:], strict=False)), "server invocation drift")
        require(group[-1]["at"] < before_close, "server invocation only finished during fallback cleanup")
        if case == "approval_unattended":
            require([row["event"] for row in group] == ["entered", "role_refused"], "unattended fixture prompted or acted")
            continue
        request = group[1]
        require(request["event"] == ("data_requested" if case == "data_form_never" else "requested"), "missing typed request witness")
        params = request["wire"]["params"] if case != "data_form_never" else None
        if params is not None:
            require(request["wire"]["method"] == "elicitation/create", "wrong SDK request route")
            check_schema(params, case)
        if asks:
            ask = asks[index]["params"]
            require((ask["threadId"], ask["turnId"], ask["serverName"]) == (thread, turns[index], "fixture")
                    and instant(request["at"]) < instant(observed[index]["at"]), "unbound native request")
            check_schema(ask, case)
            require(ask["message"] == (params["message"] if params else request["message"])
                    and entry["nonce"] in ask["message"]
                    and (params is None or same(ask["requestedSchema"], params["requestedSchema"])), "native form changed action or schema")
        effects = [row for row in group if row["event"] == "appended"]
        wanted = case in POSITIVE or case == "data_form_never"
        require(len(effects) == int(wanted), "server append witness disagrees with physical sink")
        if wanted:
            require([row["event"] for row in group] == ["entered", request["event"], "response", "appended"],
                    "positive invocation contained an unexplained terminal")
            response = next(row for row in group if row["event"] == "response")
            expected = response_for(case)
            require(same(response["response"], expected) and normal[index]["at"] < response["at"], "server did not receive its own explicit reply")
            if case == "approval_duplicate_reply":
                before_duplicate = next(row["at"] for row in evidence["observations"] if row["label"] == "before-duplicate")
                require(effects[0]["at"] < before_duplicate, "first effect was not complete before replay")
            sink = effects[0]["sink"]
            require(sink == ("data-sink.jsonl" if case == "data_form_never" else "approval-sink.jsonl"), "data became approval authority")
            effect = next(row for row in lines(root / sink) if row["nonce"] == entry["nonce"])
            require(response["at"] < effect["at"] < effects[0]["at"] < before_close, "early or late physical append")
        elif case == "data_form_empty":
            require([row["event"] for row in group] == ["entered", "requested", "response", "refused"]
                    and same(group[2]["response"], {"action": "accept", "content": {}}), "empty form was mistaken for consent")
        elif case == "approval_never":
            require([row["event"] for row in group] == ["entered", "requested", "response", "refused"]
                    and group[2]["response"].get("action") == "decline", "proper Never approval was not declined")
        elif case != "approval_eof" and turn_statuses(case)[index] == "completed":
            require([row["event"] for row in group] == ["entered", "requested", "response", "refused"], "negative fixture did not complete its refusal")
            expected = {key: value for key, value in response_for(case).items() if value is not None}
            if case == "approval_missing_content":
                # Native run 42566237032 normalized omitted accept content to
                # an empty object. It still must refuse, with no physical write.
                expected["content"] = {}
            require(same(group[2]["response"], expected) and normal[reply_slots(case).index(index)]["at"] < group[2]["at"], "negative response changed in transport")
        else:
            check_retired_request(case, evidence, group, index)
    require(all(row["nonce"] in {entry["nonce"] for entry in entered} for row in evidence["server"]), "orphan server event")


def check_case(case, root, evidence):
    try:
        _check_case(case, root, evidence)
    except (KeyError, TypeError, StopIteration, IndexError) as error:
        raise ValueError("missing or malformed elicitation witness") from error


def readback(manifest, report, artifacts, *, head, base, run_id, attempt):
    require(manifest["schema"] == "codex-elicitation/v1" and manifest["head"] == head
            and manifest["base"] == base and manifest["run_id"] == run_id and manifest["attempt"] == attempt,
            "stale native qualification identity")
    require(report["manifest_digest"] == digest(manifest), "unbound qualification report")
    required = {f"{PREFIX}[{case}]" for case in CASES}
    require(set(manifest["cases"]) == required and len(manifest["cases"]) == len(required)
            and set(report["cases"]) == required, "incomplete native case census")
    require(type(report["exitstatus"]) is int and report["exitstatus"] == 0, "failed native run")
    require(report["artifacts"] == inventory(artifacts, (".",)), "native artifact inventory drift")
    receipts = set()
    for nodeid, row in report["cases"].items():
        require(row["phases"] == [[phase, "passed"] for phase in ("setup", "call", "teardown")], "partial or failed native phase")
        path = Path(row["receipt"])
        require(not path.is_absolute() and ".." not in path.parts and str(path) not in receipts
                and str(path) in report["artifacts"] and path.name == "elicitation-evidence.json", "invalid native receipt")
        receipts.add(str(path))
        check_case(nodeid[len(PREFIX) + 1:-1], (artifacts / path).parent, load_json((artifacts / path).read_bytes()))
    return {"status": "complete", "case_total": len(required), "fixture_consent": True,
            "production_ready": False, "human_frontend": "unmeasured", "head": head, "base": base,
            "run_id": run_id, "attempt": attempt}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "report", "artifacts"):
        parser.add_argument(name, type=Path)
    for name in ("head", "base", "run-id", "attempt"):
        parser.add_argument(f"--{name}", required=True)
    args = parser.parse_args()
    manifest = load_json(args.manifest.read_bytes())
    require(manifest["sources"] == inventory(Path(__file__).resolve().parents[2], INVENTORY), "source snapshot drift")
    print(json.dumps(readback(manifest, load_json(args.report.read_bytes()), args.artifacts,
                             head=args.head, base=args.base, run_id=args.run_id, attempt=args.attempt), sort_keys=True))


if __name__ == "__main__":
    main()
