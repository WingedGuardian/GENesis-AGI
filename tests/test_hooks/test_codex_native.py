"""Opt-in native integration: GENESIS_CODEX_NATIVE_TESTS=1 pytest this exact file."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_hooks.native_codex import (
    configure,
    fixture_provider,
    run_native,
    trust_fixture_hook,
)

pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1",
    reason="native binaries are a separate opt-in integration requirement",
)


@pytest.fixture
def native_binaries():
    cli = os.environ.get("GENESIS_CODEX_CLI_BIN") or shutil.which("codex")
    app = os.environ.get("GENESIS_CODEX_APP_SERVER_BIN") or cli
    assert cli and app, "opt-in native checks require both binaries"
    versions = {}
    for client, binary in (("cli", cli), ("app", app)):
        assert Path(binary).is_file(), binary
        result = subprocess.run(  # noqa: S603
            [binary, "--version"], capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, result.stderr
        versions[client] = result.stdout.strip()
    return {"cli": cli, "app": app}, versions


def action_item(action):
    patch = "*** Begin Patch\n*** Add File: patch-receipt.txt\n+fixture\n*** End Patch"
    if action in {"mcp", "code_shell", "code_patch"}:
        program = {
            "mcp": "text(await tools.mcp__fixture__record({}));",
            "code_shell": 'text(await tools.exec_command({cmd: "printf fixture > shell-receipt.txt"}));',
            "code_patch": f"text(await tools.apply_patch({json.dumps(patch)}));",
        }[action]
        return {
            "type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
            "namespace": "functions", "name": "exec",
            "input": program,
        }
    if action == "patch":
        return {
            "type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
            "name": "apply_patch",
            "input": patch,
        }
    name = "exec_command"
    args = {"cmd": "printf fixture > shell-receipt.txt"}
    return {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
            "name": name, "arguments": json.dumps(args)}


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("action", ["shell", "patch", "mcp", "code_shell", "code_patch"])
@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_native_hook_controls_actual_sink(tmp_path, native_binaries, client, action, decision):
    binaries, versions = native_binaries
    evidence = {
        "versions": versions, "client": client, "action": action, "decision": decision,
    }
    try:
        with fixture_provider(action_item(action)) as (url, requests):
            evidence["requests"] = requests
            env = configure(tmp_path, url, decision)
            evidence["trust"] = trust_fixture_hook(binaries["app"], tmp_path, env)
            events, stderr = run_native(binaries[client], client, tmp_path, env, evidence)
            evidence.update(events=events, stderr=stderr)
    finally:
        (tmp_path / "native-evidence.json").write_text(json.dumps(evidence, indent=2))
    assert not list((tmp_path / "codex" / ".tmp").glob("plugins-clone-*")), "remote plugin sync attempted"
    assert len(requests) == 2, "fixture must request one action then finish"
    hooks = [json.loads(line) for line in (tmp_path / "hooks.jsonl").read_text().splitlines()]
    assert len(hooks) == 1, hooks
    hook = hooks[0]
    assert hook["hook_event_name"] == "PreToolUse", hook
    sink = action.removeprefix("code_")
    expected_name = {"shell": "Bash", "patch": "apply_patch", "mcp": "mcp__fixture__record"}
    assert hook["tool_name"] == expected_name[sink], hook
    if sink in {"shell", "patch"}:
        assert "command" in hook["tool_input"], hook
    receipt = tmp_path / "workspace" / f"{sink}-receipt.txt"
    if decision == "allow":
        assert receipt.read_text().strip() == "fixture"
    else:
        assert not receipt.exists(), "a denied native action must not reach its sink"
