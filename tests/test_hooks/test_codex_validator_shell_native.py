"""Opt-in closed request boundary against an explicitly supplied source runtime.

Requires GENESIS_CODEX_NATIVE_TESTS=1 and GENESIS_CODEX_VALIDATOR_RUNTIME. Copies
the actual source bytes into a private fake-provider runtime; never deploys it.
Retain qualification receipts with --basetemp and -o tmp_path_retention_policy=all.
"""

import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path

import pytest

from tests.test_hooks.native_codex import (
    configure,
    fixture_provider,
    run_native,
    trust_fixture_hook,
)
from tests.test_hooks.test_codex_native import native_binaries as _native_binaries

native_binaries = _native_binaries

pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1",
    reason="native binaries and an explicit validator runtime are required",
)
TOKEN = "00000000-0000-0000-0000-000000000001"
PROBE = {"version": 1, "operation": "protocol_probe"}
FILES = (
    "scripts/codex_validator_request.py",
    "scripts/hooks/codex-validator-guard",
    "scripts/hooks/codex_validator_guard.py",
    "scripts/hooks/codex_validator_shell.py",
    "scripts/hooks/codex_validator_patch.py",
    "scripts/hooks/shell_parse.py",
    "src/genesis/__init__.py",
    "src/genesis/eval/__init__.py",
    "src/genesis/eval/qualification/__init__.py",
    "src/genesis/eval/qualification/evidence.py",
)


def fixture_runtime(root, evidence, case):
    supplied = os.environ.get("GENESIS_CODEX_VALIDATOR_RUNTIME")
    assert supplied, "opt-in native checks require an explicit source runtime"
    source = Path(supplied).resolve(strict=True)
    runtime = root / "runtime's space"
    evidence["source_runtime"] = str(source)
    evidence["source_hashes"] = {}
    for relative in FILES:
        original = source / relative
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        target.chmod(original.stat().st_mode & 0o777)
        evidence["source_hashes"][relative] = hashlib.sha256(original.read_bytes()).hexdigest()
        assert target.read_bytes() == original.read_bytes()
    interpreter = runtime / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    # A trusted private fixture interpreter, never an editable install.
    interpreter.symlink_to(sys.executable)
    if case == "removed_gate":
        policy = runtime / "scripts/hooks/codex_validator_shell.py"
        before = policy.read_text()
        after = before.replace("    if any(ord(ch) < 32", "    return\n    if any(ord(ch) < 32", 1)
        assert before != after and after.replace("    return\n    if any(ord(ch) < 32", "    if any(ord(ch) < 32", 1) == before
        policy.write_text(after)
        evidence["mutation"] = {"file": str(policy), "before": hashlib.sha256(before.encode()).hexdigest(),
                                "after": hashlib.sha256(after.encode()).hexdigest(), "only_change": "early return in check_command"}
    return runtime


def instrument_hook(root, runtime):
    hook = root / "hook.py"
    hook.write_text(
        "import json,os,pathlib,subprocess,sys\n"
        "raw=sys.stdin.buffer.read()\n"
        f"command={['/bin/bash', str(runtime / 'scripts/hooks/codex-validator-guard'), '--runtime-root', str(runtime), '--workspace-root', str(root / 'workspace')]!r}\n"
        "env=dict(os.environ)\n"
        "env['PATH']=str(pathlib.Path(sys.executable).parent)+os.pathsep+env['PATH']\n"
        "p=subprocess.run(command,input=raw,capture_output=True,env=env,timeout=10)\n"
        f"receipt=pathlib.Path({str(root / 'hooks.jsonl')!r})\n"
        "with receipt.open('a') as f:f.write(json.dumps({'payload':json.loads(raw),'exit':p.returncode,'stdout':p.stdout.decode(),'stderr':p.stderr.decode()})+'\\n')\n"
        "sys.stderr.buffer.write(p.stderr)\n"
        "sys.exit(p.returncode)\n"
    )


def native_item(case, runtime, workspace, request):
    if case == "noop":
        return {"type": "message", "id": "msg_noop", "role": "assistant",
                "content": [{"type": "output_text", "text": "fixture complete", "annotations": []}]}
    if case in {"create_request", "update_request", "delete_request"}:
        if case == "create_request":
            body = f"*** Add File: {request}\n+{json.dumps(PROBE)}"
        elif case == "update_request":
            body = f"*** Update File: {request}\n@@\n-{json.dumps(PROBE)}\n+{json.dumps(PROBE, sort_keys=True)}"
        else:
            body = f"*** Delete File: {request}"
        patch = "*** Begin Patch\n" + body + "\n*** End Patch"
        return {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
                "name": "apply_patch", "input": patch}
    command = shlex.join([str(runtime / ".venv/bin/python"), "-I",
                          str(runtime / "scripts/codex_validator_request.py"),
                          "--workspace-root", str(workspace), "--request", str(request)])
    if case in {"excluded_shell", "removed_gate"}:
        command = "printf fixture > shell-receipt.txt"
    return {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
            "name": "exec_command", "arguments": json.dumps({"cmd": command})}


def completed_commands(events):
    commands = []
    for event in events:
        item = event.get("item") or event.get("params", {}).get("item", {})
        if item.get("type") in {"command_execution", "commandExecution"}:
            exit_code = item.get("exit_code", item.get("exitCode"))
            if exit_code is not None:
                commands.append((exit_code, item.get("aggregated_output", item.get("aggregatedOutput", ""))))
    return commands


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("case", ["probe", "unknown_request", "excluded_shell", "removed_gate", "noop", "create_request", "update_request", "delete_request", "public_request"])
def test_native_request_boundary(tmp_path, native_binaries, client, case):
    binaries, versions = native_binaries
    evidence = {"versions": versions, "client": client, "case": case}
    previous = os.umask(0o077)
    try:
        runtime = fixture_runtime(tmp_path, evidence, case)
        workspace = tmp_path / "workspace"
        request = workspace / "requests" / (TOKEN + ".json")
        item = native_item(case, runtime, workspace, request)
        with fixture_provider(item) as (url, requests):
            evidence["requests"] = requests
            env = configure(tmp_path, url, "allow")
            request.parent.mkdir(mode=0o700)
            if case != "create_request":
                request.write_text(json.dumps(PROBE if case != "unknown_request" else {"version": 1, "operation": "shell", "command": "private marker"}))
            if case == "public_request":
                request.chmod(0o644)
            instrument_hook(tmp_path, runtime)
            evidence["trust"] = trust_fixture_hook(binaries["app"], tmp_path, env)
            events, stderr = run_native(binaries[client], client, tmp_path, env, evidence)
            evidence.update(events=events, stderr=stderr)
    finally:
        os.umask(previous)
        receipt = tmp_path / "native-evidence.json"
        receipt.write_text(json.dumps(evidence, indent=2))
        receipt.chmod(0o600)
    assert len(requests) == (1 if case == "noop" else 2)
    hooks_path = tmp_path / "hooks.jsonl"
    hooks = [json.loads(line) for line in hooks_path.read_text().splitlines()] if hooks_path.exists() else []
    assert len(hooks) == (0 if case == "noop" else 1), hooks
    if hooks:
        assert hooks[0]["payload"]["hook_event_name"] == "PreToolUse"
        assert hooks[0]["stdout"] == ""
        assert hooks[0]["exit"] == (2 if case in {"excluded_shell", "public_request"} else 0)
        assert hooks[0]["payload"]["tool_name"] == ("apply_patch" if case.endswith("_request") and case in {"create_request", "update_request", "delete_request"} else "Bash")
    sink = workspace / "shell-receipt.txt"
    assert sink.exists() == (case == "removed_gate")
    if sink.exists():
        assert sink.read_text() == "fixture"
    if case == "probe":
        assert any(code == 0 and json.dumps({**PROBE, "ok": True}, sort_keys=True) in output for code, output in completed_commands(events)), events
    if case == "unknown_request":
        assert any(code == 2 and "Validator request refused" in output for code, output in completed_commands(events)), events
    if case in {"create_request", "update_request"}:
        assert json.loads(request.read_text()) == PROBE
        assert request.stat().st_mode & 0o777 == 0o600
    assert request.exists() == (case != "delete_request")
    assert not list((tmp_path / "codex" / ".tmp").glob("plugins-clone-*"))
