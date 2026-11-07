"""Opt-in closed request boundary against an explicitly supplied source runtime.

Requires GENESIS_CODEX_NATIVE_TESTS=1 and GENESIS_CODEX_VALIDATOR_RUNTIME. Copies
the actual source bytes into a private fake-provider runtime; never deploys it.
Retain qualification receipts with --basetemp and -o tmp_path_retention_policy=all.
"""

import hashlib
import json
import os
import shlex
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
        # One read: the copy and its recorded hash come from the same bytes. Each
        # case copies afresh, so compare receipts' source_hashes across a matrix.
        data = original.read_bytes()
        target.write_bytes(data)
        target.chmod(original.stat().st_mode & 0o777)
        evidence["source_hashes"][relative] = hashlib.sha256(data).hexdigest()
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
        return patch_item(body)
    command = sealed_command(runtime, workspace, request)
    if case in {"excluded_shell", "removed_gate"}:
        command = SINK_COMMAND
    return shell_item(command)


SINK_COMMAND = "printf fixture > shell-receipt.txt"


def sealed_command(runtime, workspace, request):
    return shlex.join([str(runtime / ".venv/bin/python"), "-I",
                       str(runtime / "scripts/codex_validator_request.py"),
                       "--workspace-root", str(workspace), "--request", str(request)])


def patch_item(body):
    return {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
            "name": "apply_patch", "input": "*** Begin Patch\n" + body + "\n*** End Patch"}


def shell_item(command):
    return {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture",
            "name": "exec_command", "arguments": json.dumps({"cmd": command})}


def unified_item(item):
    """Re-issue a direct shell or patch item through the unified functions.exec route."""
    if item["type"] == "function_call":
        cmd = json.loads(item["arguments"])["cmd"]
        program = f"text(await tools.exec_command({{cmd: {json.dumps(cmd)}}}));"
    else:
        program = f"text(await tools.apply_patch({json.dumps(item['input'])}));"
    return {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
            "namespace": "functions", "name": "exec", "input": program}


def completed_commands(events):
    commands = []
    for event in events:
        item = event.get("item") or event.get("params", {}).get("item", {})
        if item.get("type") in {"command_execution", "commandExecution"}:
            exit_code = item.get("exit_code", item.get("exitCode"))
            if exit_code is not None:
                commands.append((exit_code, item.get("aggregated_output", item.get("aggregatedOutput", ""))))
    return commands


def run_native_case(tmp_path, native_binaries, client, param, build_item, prepare):
    """Run one native session; build_item(runtime, workspace, request) and prepare(request) shape it.

    The receipt keeps the full parameter (code_* included) and names the transport,
    so a retained receipt shows which route was qualified.
    """
    binaries, versions = native_binaries
    unified = param.startswith("code_")
    evidence = {"versions": versions, "client": client, "case": param,
                "transport": "functions.exec" if unified else "direct"}
    previous = os.umask(0o077)
    try:
        runtime = fixture_runtime(tmp_path, evidence, param.removeprefix("code_"))
        workspace = tmp_path / "workspace"
        request = workspace / "requests" / (TOKEN + ".json")
        item = build_item(runtime, workspace, request)
        with fixture_provider(item) as (url, requests):
            evidence["requests"] = requests
            env = configure(tmp_path, url, "allow")
            request.parent.mkdir(mode=0o700)
            prepare(request)
            instrument_hook(tmp_path, runtime)
            evidence["trust"] = trust_fixture_hook(binaries["app"], tmp_path, env)
            events, stderr = run_native(binaries[client], client, tmp_path, env, evidence)
            evidence.update(events=events, stderr=stderr)
    finally:
        os.umask(previous)
        receipt = tmp_path / "native-evidence.json"
        receipt.write_text(json.dumps(evidence, indent=2))
        receipt.chmod(0o600)
    hooks_path = tmp_path / "hooks.jsonl"
    hooks = [json.loads(line) for line in hooks_path.read_text().splitlines()] if hooks_path.exists() else []
    return requests, hooks, events, workspace, request


# Request bodies the request script must refuse inside an admitted command. A
# nonfinite number is not here: the schema already rejects any non-integer
# version, so that case would pass even with a permissive decoder.
REFUSED_BODIES = {
    "unknown_request": json.dumps({"version": 1, "operation": "shell", "command": "private marker"}),
    "duplicate_key_request": '{"version": 1, "operation": "shell", "operation": "protocol_probe"}',
    "empty_request": "",
    "non_object_request": json.dumps([PROBE]),
}


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("case", ["probe", "unknown_request", "excluded_shell", "removed_gate", "noop", "create_request", "update_request", "delete_request", "public_request",
                                  "duplicate_key_request", "empty_request", "non_object_request",
                                  "code_probe", "code_update_request"])
def test_native_request_boundary(tmp_path, native_binaries, client, case):
    # code_* cases send the base case's action through the unified functions.exec route.
    param = case
    unified = case.startswith("code_")
    case = case.removeprefix("code_")

    def build(runtime, workspace, request):
        item = native_item(case, runtime, workspace, request)
        return unified_item(item) if unified else item

    def prepare(request):
        if case != "create_request":
            request.write_text(REFUSED_BODIES.get(case, json.dumps(PROBE)))
        if case == "public_request":
            request.chmod(0o644)

    requests, hooks, events, workspace, request = run_native_case(tmp_path, native_binaries, client, param, build, prepare)
    assert len(requests) == (1 if case == "noop" else 2)
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
    if case in REFUSED_BODIES:
        assert request.read_text() == REFUSED_BODIES[case]
        assert any(code == 2 and "Validator request refused" in output for code, output in completed_commands(events)), events
    if case in {"create_request", "update_request"}:
        assert json.loads(request.read_text()) == PROBE
        # Exact bytes: the update rewrites to sorted-key form, so a patch that is
        # admitted but silently no-ops leaves the original bytes and fails here.
        expected = json.dumps(PROBE, sort_keys=(case == "update_request"))
        assert json.dumps(PROBE) != json.dumps(PROBE, sort_keys=True), "fixture must make the update observable"
        assert request.read_text().rstrip("\n") == expected
        assert request.stat().st_mode & 0o777 == 0o600
    assert request.exists() == (case != "delete_request")
    assert not list((tmp_path / "codex" / ".tmp").glob("plugins-clone-*"))


def _protected(workspace):
    return workspace / "AGENTS.md"


def _write_probe(path):
    path.write_text(json.dumps(PROBE))


def _symlink_request(request):
    outside = request.parents[2] / "outside.json"
    _write_probe(outside)
    request.symlink_to(outside)


def _hardlink_request(request):
    _write_probe(request)
    os.link(request, request.parents[2] / "alias.json")


# name -> (item builder, request preparation). Every case must be refused at the
# hook (exit 2) and leave the workspace exactly as prepared.
HOOK_REFUSALS = {
    "patch_protected_target": (
        lambda rt, ws, rq: patch_item(f"*** Add File: {_protected(ws)}\n+fixture"), lambda rq: None),
    "patch_mixed_targets": (
        lambda rt, ws, rq: patch_item(f"*** Add File: {rq}\n+{json.dumps(PROBE)}\n*** Add File: {_protected(ws)}\n+fixture"),
        lambda rq: None),
    "compound_sink_after": (
        lambda rt, ws, rq: shell_item(f"{sealed_command(rt, ws, rq)}; {SINK_COMMAND}"), _write_probe),
    "compound_sink_before": (
        lambda rt, ws, rq: shell_item(f"{SINK_COMMAND}; {sealed_command(rt, ws, rq)}"), _write_probe),
    "symlink_request": (lambda rt, ws, rq: shell_item(sealed_command(rt, ws, rq)), _symlink_request),
    "hardlink_request": (lambda rt, ws, rq: shell_item(sealed_command(rt, ws, rq)), _hardlink_request),
}


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("case", [*HOOK_REFUSALS, "code_patch_protected_target"])
def test_native_hook_refusal(tmp_path, native_binaries, client, case):
    param = case
    unified = case.startswith("code_")
    case = case.removeprefix("code_")
    build_item, prepare = HOOK_REFUSALS[case]

    def build(runtime, workspace, request):
        item = build_item(runtime, workspace, request)
        return unified_item(item) if unified else item

    requests, hooks, events, workspace, request = run_native_case(tmp_path, native_binaries, client, param, build, prepare)
    assert len(requests) == 2
    assert len(hooks) == 1, hooks
    assert hooks[0]["payload"]["hook_event_name"] == "PreToolUse"
    assert hooks[0]["payload"]["tool_name"] == ("apply_patch" if case.startswith("patch_") else "Bash")
    assert hooks[0]["exit"] == 2
    assert hooks[0]["stdout"] == ""
    assert "Validator action evaluation failed" in hooks[0]["stderr"]
    # Nothing ran: no completed command, no sink, no protected file.
    assert completed_commands(events) == [], events
    assert not (workspace / "shell-receipt.txt").exists()
    assert not _protected(workspace).exists()
    if case.startswith("patch_"):
        assert not request.exists()
    elif case == "symlink_request":
        assert request.is_symlink() and request.read_text() == json.dumps(PROBE)
    else:
        assert not request.is_symlink() and request.read_text() == json.dumps(PROBE)
        assert request.stat().st_nlink == (2 if case == "hardlink_request" else 1)
    assert not list((tmp_path / "codex" / ".tmp").glob("plugins-clone-*"))
