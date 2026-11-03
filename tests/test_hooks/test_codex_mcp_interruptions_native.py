"""No replay of uncertain fixture writes; graceful EOF releases owned children."""

import json
import os
import select
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import pytest

from scripts.mcp_process_report import proc_identity, snapshot
from tests.test_hooks.native_codex import AppServer, configure, fixture_provider
from tests.test_hooks.test_codex_mcp_transport_native import FIXTURE, Service, rpc

pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1", reason="installed-client qualification only",
)


@pytest.mark.parametrize("stateful", [False, True], ids=["stateless", "stateful"])
@pytest.mark.parametrize("failure_mode", ["before", "after"])
def test_lost_reply_is_not_replayed(tmp_path, stateful, failure_mode):
    binary = os.environ.get("GENESIS_CODEX_APP_SERVER_BIN")
    assert binary and Path(binary).is_absolute() and Path(binary).is_file()
    root = tmp_path / "interrupted write"
    root.mkdir()
    evidence = {"stateful": stateful, "failure_mode": failure_mode}
    with fixture_provider({}) as (url, requests):
        env = configure(root, url, "allow")
        env["FIXTURE_TOKEN"] = "fixture-local-only"
        service = Service(root, env, stateful)
        cleanup = ExitStack()
        cleanup.callback(service.close)
        try:
            service.start(failure_mode=failure_mode)
            config = root / "codex/config.toml"
            config.write_text(config.read_text().split("[[hooks.PreToolUse]]")[0] + f'''
[mcp_servers.fixture]
url = {json.dumps(service.url)}
bearer_token_env_var = "FIXTURE_TOKEN"
startup_timeout_sec = 10
tool_timeout_sec = 5
default_tools_approval_mode = "approve"
''')
            driver = AppServer(binary, root, env)
            cleanup.callback(driver.close)
            driver.initialize()
            thread = driver.request("thread/start", {"cwd": str(root / "workspace"), "ephemeral": True}, 2)["thread"]["id"]
            params = {"threadId": thread, "server": "fixture", "tool": "echo", "arguments": {"value": "healthy"}}
            healthy = driver.request("mcpServer/tool/call", params, 3)
            assert not healthy.get("isError"), healthy

            def restart_after_exit():
                # Bound a fixture that never receives the deliberate fault call.
                code = service.process.wait(timeout=30)
                assert code == (71 if failure_mode == "before" else 72), code
                service.stop()
                service.start()

            with ThreadPoolExecutor(max_workers=1) as pool:
                restarted = pool.submit(restart_after_exit)
                evidence["interrupted"] = rpc(driver, "mcpServer/tool/call", {
                    **params, "tool": "commit", "arguments": {"value": "exactly-one-submission"},
                }, 4)
                restarted.result(timeout=30)
            interrupted = evidence["interrupted"]
            assert "error" in interrupted or interrupted.get("result", {}).get("isError") is True, interrupted
            # Never resubmit commit. If the native client did, the durable
            # scratch ledger catches it even when the response was lost.
            evidence["reload"] = rpc(driver, "config/mcpServer/reload", {}, 5)
            assert "result" in evidence["reload"], evidence["reload"]
            evidence["read_count"] = rpc(driver, "mcpServer/tool/call", {
                **params, "tool": "read_count", "arguments": {},
            }, 6)
            ledger = root / "writes.jsonl"
            evidence["writes"] = [json.loads(line) for line in ledger.read_text().splitlines()] if ledger.exists() else []
            expected = 0 if failure_mode == "before" else 1
            assert len(evidence["writes"]) == expected, evidence
            assert json.loads(evidence["read_count"]["result"]["content"][0]["text"]) == expected, evidence
        finally:
            try:
                cleanup.close()
            finally:
                evidence["model_requests"] = len(requests)
                print("MCP_INTERRUPTION_EVIDENCE " + json.dumps(evidence, sort_keys=True), flush=True)
        assert not requests


def test_stdio_eof_releases_fixture_children(tmp_path):
    binary = os.environ.get("GENESIS_CODEX_APP_SERVER_BIN")
    assert binary and Path(binary).is_absolute() and Path(binary).is_file()
    root = tmp_path / "stdio eof"
    root.mkdir()
    evidence = {}
    with fixture_provider({}) as (url, requests):
        env = configure(root, url, "allow")
        config = root / "codex/config.toml"
        config.write_text(config.read_text().split("[[hooks.PreToolUse]]")[0] + f'''
[mcp_servers.fixture]
command = {json.dumps(sys.executable)}
args = [{json.dumps(str(FIXTURE))}]
startup_timeout_sec = 10
tool_timeout_sec = 5
default_tools_approval_mode = "approve"
''')
        driver = AppServer(binary, root, env)
        try:
            driver.initialize()
            thread = driver.request("thread/start", {"cwd": str(root / "workspace"), "ephemeral": True}, 2)["thread"]["id"]
            result = driver.request("mcpServer/tool/call", {
                "threadId": thread, "server": "fixture", "tool": "echo", "arguments": {"value": "stdio"},
            }, 3)
            assert not result.get("isError"), result
            fixture_pid = json.loads(result["content"][0]["text"])["pid"]
            observed = snapshot()
            evidence["discovery_errors"] = observed["discovery_errors"]
            before = [r for r in observed["processes"] if r["daemon_pid"] == driver.process.pid and r["pid"] != driver.process.pid]
            assert any(r["pid"] == fixture_pid and r["stable"] for r in before), "exact responding fixture must be tracked"
            assert all(r["stable"] for r in before), "unstable child observation cannot prove cleanup"
            evidence["responding_fixture_pid"] = fixture_pid
            evidence["children_before"] = [{"pid": r["pid"], "start_ticks": r["start_ticks"]} for r in before]
            driver.process.stdin.close()
            # Observe exit without reaping: driver.close() still group-signals
            # this leader, whose PID must remain reserved until that happens.
            pidfd = os.pidfd_open(driver.process.pid)
            try:
                assert select.select([pidfd], [], [], 30)[0], "app-server did not exit on stdio EOF"
                status = os.waitid(os.P_PID, driver.process.pid, os.WEXITED | os.WNOWAIT)
                evidence["exit_code"] = status.si_status if status.si_code == os.CLD_EXITED else -status.si_status
                assert evidence["exit_code"] == 0, evidence
            finally:
                os.close(pidfd)
            time.sleep(5)
            survivors = []
            for row in before:
                try:
                    _, ticks = proc_identity(Path("/proc") / str(row["pid"]))
                except FileNotFoundError:
                    continue
                if ticks == row["start_ticks"]:
                    survivors.append(row["pid"])
            evidence["survivors_after_five_seconds"] = survivors
            assert not survivors, evidence
        finally:
            try:
                driver.close()
            finally:
                evidence["model_requests"] = len(requests)
                print("MCP_STDIO_EOF_EVIDENCE " + json.dumps(evidence, sort_keys=True), flush=True)
        assert not requests
