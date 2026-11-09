"""Contained compatibility measurements; fixture consent is never owner consent."""

import hashlib
import json
import os
import select
import shlex
import sys
import time
from pathlib import Path

import pytest

from scripts.ci.codex_qualification_readback import (
    ACCEPTED_STEER,
    APPROVAL_DECISIONS,
    ASK_MODES,
    CLIENTS,
    REJECTED_STEER,
)
from tests.test_hooks.native_codex import (
    AppServer,
    configure,
    fixture_provider,
    run_native,
    trust_fixture_hook,
)
from tests.test_hooks.test_codex_native import native_binaries as native_binaries

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1", reason="contained native qualification only",
)


@pytest.fixture
def pinned(native_binaries):
    binaries, versions = native_binaries
    assert set(versions.values()) == {"codex-cli 0.166.0"}, versions
    companion = Path(binaries["app"]).with_name("codex-code-mode-host")
    assert companion.is_file(), "retain the companion from the same verified package"
    return binaries


@pytest.fixture
def evidence(tmp_path):
    record = {"fixture_consent": True, "production_ready": False}
    yield record
    # Teardown preserves incomplete evidence too; only the readback decides completeness.
    record["files"] = {
        str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(tmp_path.rglob("*"))
        if path.is_file() and path.name != "compat-evidence.json"
    }
    (tmp_path / "compat-evidence.json").write_text(json.dumps(record, indent=2))


def begin(driver, *, persistent=False, approvals=False):
    driver.initialize()
    thread = driver.request("thread/start", {
        "cwd": str(driver.root / "workspace"), "ephemeral": not persistent,
    }, 2)["thread"]["id"]
    turn = driver.request("turn/start", {
        "threadId": thread, "input": [{"type": "text", "text": "Run the isolated fixture."}],
    }, 3, allow_requests=approvals)["turn"]["id"]
    return thread, turn


def completed(driver, turn):
    def matches(event):
        return event.get("method") == "turn/completed" and event["params"]["turn"]["id"] == turn
    return next((event for event in driver.events if matches(event)), None) or driver.until(matches)


def shell_item(command, **extra):
    return {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
            "name": "exec_command", "arguments": json.dumps({"cmd": command, **extra})}


@pytest.mark.parametrize("decision", APPROVAL_DECISIONS)
def test_native_approval(tmp_path, pinned, evidence, decision):
    sink = tmp_path / "workspace/approval.txt"
    command = f"printf fixture > {shlex.quote(str(sink))}"
    item = shell_item(command, sandbox_permissions="require_escalated", justification="Synthetic fixture approval")
    with fixture_provider(item) as (url, requests):
        env = configure(tmp_path, url, "allow")
        config = tmp_path / "codex/config.toml"
        config.write_text(config.read_text().replace('approval_policy = "never"', 'approval_policy = "on-request"')
                          .replace('sandbox_mode = "danger-full-access"', 'sandbox_mode = "read-only"'))
        trust_fixture_hook(pinned["app"], tmp_path, env)
        driver = AppServer(pinned["app"], tmp_path, env)
        driver.root = tmp_path
        evidence["events"] = driver.events
        try:
            thread, turn = begin(driver, approvals=True)
            evidence.update(thread=thread, turn=turn)
            started = next(event["result"] for event in driver.events if event.get("id") == 2 and "method" not in event)
            assert started["approvalPolicy"] == "on-request" and started["sandbox"]["type"] == "readOnly", started
            request = next((event for event in driver.events if "id" in event and "method" in event), None)
            request = request or driver.until(lambda event: "id" in event and "method" in event, allow_requests=True)
            assert request["method"] == "item/commandExecution/requestApproval", request
            assert request["params"]["threadId"] == thread and request["params"]["turnId"] == turn
            assert str(sink) in request["params"]["command"], request
            assert not sink.exists(), "native work must wait for the fixture decision"
            if decision == "disconnect":
                driver.process.stdin.close()
                # Observe native EOF shutdown before the fallback signals, without
                # reaping/releasing the leader PID that close() still owns.
                pidfd = os.pidfd_open(driver.process.pid)
                try:
                    assert select.select([pidfd], [], [], 30)[0], "native server did not exit on pending-approval EOF"
                    status = os.waitid(os.P_PID, driver.process.pid, os.WEXITED | os.WNOWAIT)
                    assert status.si_code == os.CLD_EXITED and status.si_status == 0, status
                    evidence["eof_exit"] = {"code": status.si_status, "before_forced_cleanup": True}
                finally:
                    os.close(pidfd)
            elif decision in {"interrupt", "stale"}:
                driver.request("turn/interrupt", {"threadId": thread, "turnId": turn}, 4)
                assert completed(driver, turn)["params"]["turn"]["status"] == "interrupted"
                if decision == "stale":
                    driver.reply(request, {"decision": "accept"})
                    driver.request("config/read", {"includeLayers": False}, 5)  # ordered native barrier
            else:
                if decision == "steer":
                    driver.send("turn/steer", {"threadId": thread, "expectedTurnId": "wrong-turn",
                                               "input": [{"type": "text", "text": REJECTED_STEER}]}, 4)
                    wrong = driver.until(lambda event: event.get("id") == 4, allow_error=True)
                    assert "error" in wrong, wrong
                    evidence["steer"] = driver.request("turn/steer", {
                        "threadId": thread, "expectedTurnId": turn,
                        "input": [{"type": "text", "text": ACCEPTED_STEER}],
                    }, 5)
                    assert evidence["steer"]["turnId"] == turn
                driver.reply(request, {"decision": "accept" if decision == "accept" else "decline"})
                assert completed(driver, turn)["params"]["turn"]["status"] == "completed"
        finally:
            driver.close()
            evidence.update(model_requests=requests, decision=decision, sink=sink.exists())
        assert sink.exists() == (decision == "accept")
        if decision == "steer":
            assert ACCEPTED_STEER not in json.dumps(requests[0])
            assert REJECTED_STEER not in json.dumps(requests)
            assert any(ACCEPTED_STEER in json.dumps(row) for row in requests[1:]), "accepted steering input never reached the provider"
        if sink.exists():
            assert sink.read_text() == "fixture"


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("mode", ASK_MODES)
def test_canonical_ask_route(tmp_path, pinned, evidence, client, mode):
    """Compare the real secrets guard, its native wire, and existing deny parser."""
    with fixture_provider(shell_item("placeholder")) as (url, requests):
        env = configure(tmp_path, url, "allow")
        workspace = tmp_path / "workspace"
        target = workspace / ("ordinary.txt" if mode == "safe" else "secrets.env")
        target.write_text("synthetic fixture only\n")
        command = f"cat {shlex.quote(str(target))}; printf fixture > canonical-receipt.txt"
        # fixture_provider keeps the same item reference; replace it before any call.
        item = shell_item(command)
        # A second local provider avoids mutating shared fixture driver behavior.
        with fixture_provider(item) as (action_url, action_requests):
            config = tmp_path / "codex/config.toml"
            config.write_text(config.read_text().replace(url, action_url))
            wrapper = tmp_path / "hook.py"
            wrapper.write_text(
                "import json, subprocess, sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(ROOT / 'scripts/hooks')!r})\n"
                "from codex_validator_adapter import child_result\n"
                "payload = sys.stdin.read()\n"
                f"r = subprocess.run([{sys.executable!r}, {str(ROOT / 'scripts/hooks/secrets_env_access_guard.py')!r}], "
                "input=payload, text=True, capture_output=True, timeout=8)\n"
                f"Path({str(tmp_path / 'guard-result.json')!r}).write_text(json.dumps(dict(code=r.returncode, stdout=r.stdout, stderr=r.stderr)))\n"
                + ("try: child_result(r.returncode, r.stdout.encode(), r.stderr.encode())\n"
                   "except Exception as exc: print(str(exc), file=sys.stderr); sys.exit(2)\n"
                   if mode == "adapted-ask" else "print(r.stdout, end=''); sys.exit(r.returncode)\n")
            )
            evidence["trust"] = trust_fixture_hook(pinned["app"], tmp_path, env)
            evidence["events"], evidence["stderr"] = run_native(pinned[client], client, tmp_path, env, evidence)
            evidence["model_requests"] = action_requests
        assert not requests
    result = json.loads((tmp_path / "guard-result.json").read_text())
    assert result["code"] == 0, result
    if mode != "safe":
        assert json.loads(result["stdout"])["hookSpecificOutput"]["permissionDecision"] == "ask"
    else:
        assert result["stdout"] == "", result
    allowed = mode != "adapted-ask"
    evidence.update(client=client, mode=mode, canonical_same_session_approval="unproven", sink=(workspace / "canonical-receipt.txt").exists())
    assert evidence["sink"] == allowed
    if allowed:
        assert (workspace / "canonical-receipt.txt").read_text() == "fixture"


def test_native_resume(tmp_path, pinned, evidence):
    with fixture_provider(shell_item("printf fixture >> resume.txt")) as (url, requests):
        env = configure(tmp_path, url, "allow")
        trust_fixture_hook(pinned["app"], tmp_path, env)
        driver = AppServer(pinned["app"], tmp_path, env)
        driver.root = tmp_path
        try:
            thread, turn = begin(driver, persistent=True)
            evidence.update(thread=thread, turn=turn)
            assert completed(driver, turn)["params"]["turn"]["status"] == "completed"
        finally:
            driver.close()
        sink = tmp_path / "workspace/resume.txt"
        assert sink.is_file() and sink.read_bytes() == b"fixture", "initial action must execute exactly once"
        resumed = AppServer(pinned["app"], tmp_path, env)
        try:
            resumed.initialize()
            evidence["resume"] = resumed.request("thread/resume", {"threadId": thread}, 2)
            assert evidence["resume"]["thread"]["id"] == thread
        finally:
            resumed.close()
            evidence["events"] = driver.events + resumed.events
            evidence.update(resume_events=resumed.events, model_requests=requests)
        assert sink.read_bytes() == b"fixture", "resume must not replay the completed action"
        assert len(requests) == 2, "resume must not request another model response"


@pytest.mark.parametrize("failure", [False, True], ids=["provenance-cleanup", "required-failure"])
def test_required_mcp_and_owned_cleanup(tmp_path, pinned, evidence, monkeypatch, failure):
    from genesis.util.codex_cli import codex_child_env

    item = {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
            "namespace": "functions", "name": "exec", "input": "text(await tools.mcp__fixture__record({}));"}
    with fixture_provider(item) as (url, requests):
        env = configure(tmp_path, url, "allow")
        monkeypatch.setenv("GENESIS_CC_SESSION", "0")
        env["GENESIS_CC_SESSION"] = codex_child_env()["GENESIS_CC_SESSION"]
        config = tmp_path / "codex/config.toml"
        config.write_text(config.read_text().replace('required = true', 'required = true\nenv_vars = ["GENESIS_CC_SESSION"]'))
        server = tmp_path / "fixture_mcp.py"
        if failure:
            server.write_text("raise RuntimeError('deliberate required fixture failure')\n")
        else:
            child = "import os,time; from pathlib import Path; " + f"Path({str(tmp_path / 'child-ready')!r}).write_text(str(os.getpid())); time.sleep(120)"
            server.write_text(
                "import os,subprocess,sys,time\nfrom pathlib import Path\nfrom fastmcp import FastMCP\n"
                "mcp=FastMCP('fixture')\n"
                "@mcp.tool()\n"
                "def record() -> str:\n"
                f" p=subprocess.Popen([sys.executable,'-c',{child!r}], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
                " deadline=time.monotonic()+10\n"
                f" while not Path({str(tmp_path / 'child-ready')!r}).exists():\n"
                "  assert p.poll() is None and time.monotonic()<deadline\n  time.sleep(.01)\n"
                f" Path({str(tmp_path / 'marker')!r}).write_text(os.environ.get('GENESIS_CC_SESSION','missing'))\n"
                " return 'fixture'\nmcp.run()\n"
            )
        trust_fixture_hook(pinned["app"], tmp_path, env)
        driver = AppServer(pinned["app"], tmp_path, env)
        driver.root = tmp_path
        evidence["events"] = driver.events
        child_identity = None
        try:
            if failure:
                driver.initialize()
                driver.send("thread/start", {"cwd": str(tmp_path / "workspace"), "ephemeral": True}, 2)
                result = driver.until(lambda event: event.get("id") == 2, allow_error=True)
                evidence["failure_response"] = result
                assert "error" in result, result
                assert not requests, "required MCP failure must precede model work"
            else:
                thread, turn = begin(driver)
                evidence.update(thread=thread, turn=turn)
                assert completed(driver, turn)["params"]["turn"]["status"] == "completed"
                assert (tmp_path / "marker").read_text() == "1"
                from scripts.ci.ego_owned_processes import census, descendants, parse_stat
                child_pid = int((tmp_path / "child-ready").read_text())
                rows = census()
                child_identity = rows[child_pid]
                assert child_identity["state"] != "Z" and child_pid in descendants(rows, driver.process.pid)
                ancestry = [child_pid]
                while ancestry[-1] != driver.process.pid:
                    ancestry.append(rows[ancestry[-1]]["ppid"])
                evidence["owned_ancestry"] = ancestry
                evidence["driver_pid"] = driver.process.pid
                evidence["child_before"] = child_identity  # positive survivor-control witness
        finally:
            driver.close()
            evidence["model_requests"] = requests
        if child_identity:
            deadline = time.monotonic() + 5
            while True:
                try:
                    after = parse_stat(Path(f"/proc/{child_identity['pid']}/stat").read_text())
                except FileNotFoundError:
                    evidence["child_after"] = {"state": "absent"}
                    break
                if after["state"] == "Z" or after["start"] != child_identity["start"]:
                    evidence["child_after"] = after
                    break
                assert time.monotonic() < deadline, "owned ready descendant survived cleanup"
                time.sleep(.01)
            evidence["cleanup_confirmed"] = True
