"""CI-only native MCP approval transport; synthetic consent, no live authority."""

import copy
import json
import os
import select
from pathlib import Path

import pytest

from scripts.ci.codex_qualification_readback import inventory
from scripts.ci.ego_owned_processes import census, descendants, parse_stat, wait_for
from tests.test_hooks.codex_elicitation_driver import ElicitationDriver
from tests.test_hooks.native_codex import AppServer, configure, fixture_provider, trust_fixture_hook
from tests.test_hooks.test_codex_compatibility_native import pinned as pinned
from tests.test_hooks.test_codex_native import action_item
from tests.test_hooks.test_codex_native import native_binaries as native_binaries

ROOT = Path(__file__).resolve().parents[2]
CASES = (
    "approval_accept_true", "approval_decline", "approval_cancel", "approval_false",
    "approval_missing_content", "approval_bad_boolean", "approval_extra_content",
    "approval_wrong_rpc_id", "approval_duplicate_reply", "approval_interrupt",
    "approval_eof", "approval_two_requests", "approval_stale_after_interrupt",
    "approval_never", "approval_unattended", "data_form_never", "data_form_empty",
)
pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1",
    reason="native binaries are a separate opt-in integration requirement",
)


def rows(root):
    path = root / "server.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def capture_ownership(driver, root, evidence):
    population = census()
    parent = driver.transport.process.pid
    evidence.setdefault("driver_before", population[parent])
    owned_pids = descendants(population, parent)
    for row in rows(root):
        if row["event"] != "entered":
            continue
        recorded = row["process"]
        if any(item["before"]["pid"] == recorded["pid"] and item["before"]["start"] == recorded["start"]
               for item in evidence["owned"]):
            continue
        actual = population[recorded["pid"]]
        assert actual["start"] == recorded["start"] and actual["state"] != "Z"
        assert actual["pid"] in owned_pids
    for pid in sorted(owned_pids - {parent}):
        actual = population[pid]
        if any(item["before"]["pid"] == pid and item["before"]["start"] == actual["start"]
               for item in evidence["owned"]):
            continue
        ancestry = [actual["pid"]]
        while ancestry[-1] != parent:
            ancestry.append(population[ancestry[-1]]["ppid"])
        evidence["owned"].append({"before": actual, "ancestry": ancestry})


def observe_retirement(evidence):
    for item in evidence["owned"]:
        before = item["before"]

        def gone(item=item, before=before):
            try:
                after = parse_stat(Path(f"/proc/{before['pid']}/stat").read_text())
            except FileNotFoundError:
                after = {"state": "absent"}
            item["after"] = after
            return after["state"] in {"absent", "Z"} or after.get("start") != before["start"]

        wait_for(gone)  # Inherited owned-scope barrier; no new production timeout.


def complete(driver, turn, status="completed"):
    def matching(event):
        return event.get("method") == "turn/completed" and event["params"]["turn"]["id"] == turn

    event = next((row for row in driver.events if matching(row)), None)
    event = event or driver.until(matching, allow_requests=True)
    assert event["params"]["turn"]["status"] == status, event


def ask(driver, root, evidence):
    event = next(iter(driver.consents.pending.values()), None)
    event = event or driver.until(lambda row: row.get("method") == "mcpServer/elicitation/request",
                                 allow_requests=True)
    capture_ownership(driver, root, evidence)
    driver.snapshot("pending-ask")
    return copy.deepcopy(event)


def start_turn(driver, thread, request_id):
    return driver.request("turn/start", {
        "threadId": thread, "input": [{"type": "text", "text": "Run the isolated fixture."}],
    }, request_id, allow_requests=True)["turn"]["id"]


def interrupt(driver, thread, turn, request_id):
    driver.snapshot("before-interrupt")
    driver.consents.retire(turn)
    driver.request("turn/interrupt", {"threadId": thread, "turnId": turn}, request_id,
                   allow_requests=True)
    complete(driver, turn, "interrupted")
    driver.snapshot("after-interrupt")


@pytest.mark.parametrize("case", CASES)
def test_native_elicitation(tmp_path, pinned, case):
    evidence = {"schema": "codex-elicitation/v1", "case": case,
                "fixture_consent": True, "production_ready": False, "owned": [], "turns": []}
    first = action_item("mcp")
    second = {**copy.deepcopy(first), "id": "ct_fixture_second", "call_id": "call_fixture_second"}
    multi = case in {"approval_two_requests", "approval_stale_after_interrupt"}
    # An interrupted pending action has no final provider turn. Keep the actual
    # request trace, so any contrary native behavior fails rather than replaying.
    plan = {1: first, (2 if case == "approval_stale_after_interrupt" else 3): second} if multi else None
    driver = None
    requests = []
    try:
        with fixture_provider(first, action_plan=plan) as (url, requests):
            try:
                env = configure(tmp_path, url, "allow")
                env.update(ELICITATION_ROOT=str(tmp_path), ELICITATION_CASE=case,
                           GENESIS_CC_SESSION="1" if case == "approval_unattended" else "0")
                config = tmp_path / "codex/config.toml"
                text = config.read_text()
                old = json.dumps(str(tmp_path / "fixture_mcp.py"))
                assert text.count(old) == 1 and text.count("required = true") == 1
                text = text.replace(old, json.dumps(str(ROOT / "tests/test_hooks/codex_elicitation_fixture.py")))
                text = text.replace("required = true", 'required = true\nenv_vars = ["ELICITATION_ROOT", "ELICITATION_CASE", "GENESIS_CC_SESSION"]')
                text = text.replace("[analytics]", 'approvals_reviewer = "user"\n[analytics]', 1)
                never = case in {"approval_never", "data_form_never", "data_form_empty"}
                if not never:
                    text = text.replace('approval_policy = "never"', 'approval_policy = "on-request"')
                    text = text.replace('sandbox_mode = "danger-full-access"', 'sandbox_mode = "read-only"')
                config.write_text(text)
                evidence["trust"] = trust_fixture_hook(pinned["app"], tmp_path, env)
                driver = ElicitationDriver(AppServer(pinned["app"], tmp_path, env), tmp_path)
                evidence.update(events=driver.events, sent=driver.sent, observations=driver.observations,
                                observed=driver.observed, driver_pid=driver.transport.process.pid)
                driver.initialize()
                started = driver.start_thread(tmp_path / "workspace")
                evidence["started"] = started
                assert started["approvalPolicy"] == ("never" if never else "on-request")
                assert started["sandbox"]["type"] == ("dangerFullAccess" if never else "readOnly")
                thread = started["thread"]["id"]
                evidence["thread"] = thread
                turn = start_turn(driver, thread, 3)
                evidence["turns"].append(turn)
                if case in {"approval_never", "approval_unattended", "data_form_empty"}:
                    complete(driver, turn)
                    assert not driver.consents.seen, "the intended policy control must not ask"
                    capture_ownership(driver, tmp_path, evidence)
                else:
                    pending = ask(driver, tmp_path, evidence)
                    if case == "approval_eof":
                        capture_ownership(driver, tmp_path, evidence)
                        driver.transport.process.stdin.close()
                        pidfd = os.pidfd_open(driver.transport.process.pid)
                        try:
                            # Same EOF observation bound as the inherited native qualification.
                            assert select.select([pidfd], [], [], 30)[0]
                            status = os.waitid(os.P_PID, driver.transport.process.pid, os.WEXITED | os.WNOWAIT)
                            assert status.si_code == os.CLD_EXITED and status.si_status == 0
                            evidence["eof_exit"] = {"code": 0, "before_forced_cleanup": True}
                        finally:
                            os.close(pidfd)
                    elif case in {"approval_interrupt", "approval_wrong_rpc_id", "approval_stale_after_interrupt"}:
                        if case == "approval_wrong_rpc_id":
                            driver.raw_reply("foreign-fixture-request", {"action": "accept", "content": {"confirmed": True}})
                            driver.request("config/read", {"includeLayers": False}, 100, allow_requests=True)
                            driver.snapshot("after-negative-barrier")
                        interrupt(driver, thread, turn, 101)
                        if case == "approval_stale_after_interrupt":
                            second_turn = start_turn(driver, thread, 102)
                            evidence["turns"].append(second_turn)
                            ask(driver, tmp_path, evidence)
                            driver.raw_reply(pending["id"], {"action": "accept", "content": {"confirmed": True}})
                            driver.request("config/read", {"includeLayers": False}, 103, allow_requests=True)
                            driver.snapshot("after-negative-barrier")
                            interrupt(driver, thread, second_turn, 104)
                    else:
                        result = {"action": "accept", "content": {"confirmed": True}}
                        if case in {"approval_decline", "approval_cancel"}:
                            result = {"action": case.removeprefix("approval_"), "content": None}
                        elif case == "approval_false":
                            result["content"]["confirmed"] = False
                        elif case == "approval_missing_content":
                            result.pop("content")
                        elif case == "approval_bad_boolean":
                            result["content"]["confirmed"] = "true"
                        elif case == "approval_extra_content":
                            result["content"]["extra"] = True
                        elif case == "data_form_never":
                            result["content"] = {"value": True}
                        driver.reply(pending, result, thread=thread, turn=turn)
                        complete(driver, turn)
                        if case == "approval_duplicate_reply":
                            driver.snapshot("before-duplicate")
                            driver.raw_reply(pending["id"], result)
                            driver.request("config/read", {"includeLayers": False}, 100, allow_requests=True)
                            driver.snapshot("after-negative-barrier")
                        elif case == "approval_two_requests":
                            second_turn = start_turn(driver, thread, 100)
                            evidence["turns"].append(second_turn)
                            second_ask = ask(driver, tmp_path, evidence)
                            assert type(second_ask["id"]) is not type(pending["id"]) or second_ask["id"] != pending["id"]
                            driver.reply(second_ask, result, thread=thread, turn=second_turn)
                            complete(driver, second_turn)
            finally:
                if driver is not None:
                    driver.close()
                    evidence["exit_code"] = driver.transport.process.returncode
                    observe_retirement(evidence)
                    driver.snapshot("after-retirement")
    finally:
        evidence["model_requests"] = requests
        evidence["server"] = rows(tmp_path)
        evidence["files"] = {name: value for name, value in inventory(tmp_path, (".",)).items()
                             if name != "elicitation-evidence.json"}
        (tmp_path / "elicitation-evidence.json").write_text(json.dumps(evidence, indent=2))
    # The independent reader checks schema/policy/ordering and the actual files,
    # including all negative cases; an assertion-only receipt cannot qualify.
    from scripts.ci.codex_elicitation_readback import check_case

    check_case(case, tmp_path, evidence)
