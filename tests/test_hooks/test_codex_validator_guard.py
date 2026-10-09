"""Closed native action dispatch and explicit outer-result receipts."""

import builtins
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HOOKS = ROOT / "scripts" / "hooks"


@pytest.fixture
def guard(monkeypatch):
    spec = importlib.util.spec_from_file_location("validator_guard_fixture",
                                                 HOOKS / "codex_validator_guard.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    original_path = sys.path[:]
    yield module
    sys.path[:] = original_path


def payload(name, arguments):
    return {"hook_event_name": "PreToolUse", "tool_name": name, "tool_input": arguments,
            "cwd": "/untrusted", "session_id": "ignored", "role": "external"}


@pytest.mark.parametrize("name,kind", [("Bash", "shell"), ("apply_patch", "patch")])
def test_command_normalization_preserves_literal_bytes_without_context(guard, monkeypatch, name, kind):
    original_import = builtins.__import__

    def no_profile_import(module_name, *args, **kwargs):
        assert module_name != "genesis.mcp.external_profiles", "shell/patch loaded FastMCP"
        return original_import(module_name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_profile_import)
    command = "  printf '$HOME'\n# synthetic\n"
    action = guard.normalize(payload(name, {"command": command, "workdir": "/untrusted"}))
    assert action.kind == kind
    assert action.arguments == {"command": command}
    with pytest.raises(guard.Refused, match="not installed"):
        guard.dispatch(action)


@pytest.mark.parametrize("bad", [None, [], {},
    {"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "x"}},
    payload("Bash", {}), payload("Bash", {"command": 1}), payload("Bash", {"command": " "}),
    payload("exec_command", {"command": "x"}), payload("Bash", []),
    payload("mcp__foreign__health_status", {}),
    payload("mcp__genesis-validator-health__health_status", {}),
])
def test_unknown_or_malformed_actions_are_refused(guard, bad):
    with pytest.raises(guard.Refused):
        guard.normalize(bad)


def test_dispatch_uses_shared_role_floor_for_every_server_and_refuses_foreign_tools(guard):
    from genesis.mcp.external_profiles import ExternalProfile, profile_tools

    for prefix, server in guard.MCP_SERVER_PREFIXES.items():
        for tool in profile_tools(server, ExternalProfile.VALIDATOR):
            arguments = {"query": "synthetic", "nested": {"preserved": True}}
            action = guard.normalize(payload(prefix + tool, arguments))
            assert action.arguments == arguments
            guard.dispatch(action)
        for tool in ("", "record", "memory_store", "campaign_trigger", "health_status__extra"):
            with pytest.raises(guard.Refused):
                guard.dispatch(guard.normalize(payload(prefix + tool, {})))


def invoke(tmp_path, data, *, runtime=ROOT, workspace=None):
    workspace = workspace or tmp_path
    env = {"PATH": str(Path(sys.executable).parent) + os.pathsep + os.defpath,
           "HOME": str(tmp_path)}
    return subprocess.run(
        ["/bin/bash", str(HOOKS / "codex-validator-guard"),
         "--runtime-root", str(runtime), "--workspace-root", str(workspace)],
        input=data, capture_output=True, env=env, timeout=20,
    )


def test_outer_native_allow_is_empty_stdout_and_denials_do_not_echo_input(tmp_path):
    allowed = invoke(tmp_path, json.dumps(payload(
        "mcp__genesis_validator_health__health_status", {},
    )).encode())
    assert allowed.returncode == 0, allowed.stderr
    assert allowed.stdout == b""
    denied = invoke(tmp_path, json.dumps(payload("Bash", {"command": "synthetic-sensitive"})).encode())
    assert denied.returncode == 2
    assert denied.stdout == b""
    assert b"synthetic-sensitive" not in denied.stderr


@pytest.mark.parametrize("data", [b"not-json", b"{}", b'{"tool_name":"Bash","tool_name":"x"}',
                                  b'{"hook_event_name":"PreToolUse","tool_name":"mcp__genesis_validator_health__health_status","tool_input":{"key":1,"key":2}}',
                                  b'{"hook_event_name":"PreToolUse","tool_name":"mcp__genesis_validator_health__health_status","tool_input":{"value":NaN}}',
                                  b'{"hook_event_name":"PreToolUse","tool_name":"mcp__genesis_validator_health__health_status","tool_input":{"value":1e999}}',
                                  b"x" * (1024 * 1024 + 1)])
def test_outer_bad_payload_never_allows(tmp_path, data):
    result = invoke(tmp_path, data)
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"BLOCKED" in result.stderr


@pytest.mark.parametrize("context", ["wrong-runtime", "relative-runtime", "missing-workspace",
                                    "runtime-workspace", "workspace-ancestor"])
def test_payload_cannot_repair_invalid_launch_context(tmp_path, context):
    runtime = "relative" if context == "relative-runtime" else (
        tmp_path if context == "wrong-runtime" else ROOT)
    workspace = ROOT if context == "runtime-workspace" else (
        tmp_path / "absent" if context == "missing-workspace" else tmp_path)
    if context == "workspace-ancestor":
        workspace = ROOT.parent
    result = invoke(tmp_path, json.dumps(payload(
        "mcp__genesis_validator_health__health_status", {"runtime_root": str(ROOT)},
    )).encode(), runtime=runtime, workspace=workspace)
    assert result.returncode == 2
    assert result.stdout == b""


@pytest.mark.parametrize("program", ["print('unexpected')", "print('allow'); raise SystemExit(2)",
                                     "raise RuntimeError('synthetic failure')", "pass"])
def test_outer_evaluator_failures_or_missing_results_are_explicit_denials(tmp_path, program):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    wrapper = hooks / "codex-validator-guard"
    wrapper.write_bytes((HOOKS / "codex-validator-guard").read_bytes())
    (hooks / "codex_validator_guard.py").write_text(program)
    result = subprocess.run(["/bin/bash", str(wrapper)], input=b"{}", capture_output=True,
                            env={"PATH": os.defpath}, timeout=10)
    assert result.returncode == 2
    assert result.stdout == b""
    assert b"BLOCKED" in result.stderr


@pytest.mark.parametrize("program", ["print('deny'); raise SystemExit(2)",
                                     "raise RuntimeError('synthetic failure')"])
def test_wrapper_completion_receipt_is_written_for_both_denial_paths(tmp_path, program):
    wrapper = tmp_path / "codex-validator-guard"
    wrapper.write_bytes((HOOKS / "codex-validator-guard").read_bytes())
    (tmp_path / "codex_validator_guard.py").write_text(program)
    receipt = tmp_path / "completion"
    result = subprocess.run(
        ["/bin/bash", "-c", 'exec 3> "$1"; shift; exec /bin/bash "$@"',
         "fixture", str(receipt), str(wrapper)],
        input=b"{}", capture_output=True, env={"PATH": os.defpath}, timeout=10,
    )
    assert result.returncode == 2
    assert result.stdout == b""
    assert receipt.read_bytes() == b"deny\n"


@pytest.mark.skipif(os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1",
                    reason="native clients require explicit opt-in")
@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("case", ["health", "memory", "unlisted", "foreign", "failed", "shell", "patch",
                                 "code_shell", "code_patch"])
def test_native_clients_observe_guard_at_action_sink(tmp_path, client, case):
    # Native fixtures are supplied by dependency #3075, independently of #3077.
    from tests.test_hooks.native_codex import (
        configure,
        fixture_provider,
        run_native,
        trust_fixture_hook,
    )
    from tests.test_hooks.test_codex_native import action_item

    binary = shutil.which("codex")
    assert binary, "opt-in check requires native Codex"
    server = "genesis_validator_memory" if case == "memory" else "genesis_validator_health"
    if case == "foreign":
        server = "foreign"
    tool = {"health": "health_status", "memory": "memory_stats",
            "unlisted": "record", "foreign": "health_status",
            "failed": "health_status"}.get(case)
    action = action_item(case) if tool is None else {
        "type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
        "namespace": "functions", "name": "exec",
        "input": f"text(await tools.mcp__{server}__{tool}({{}}));",
    }
    evidence = {"client": client, "case": case}
    try:
        with fixture_provider(action) as (url, requests):
            env = configure(tmp_path, url, "allow")
            env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
            config = tmp_path / "codex" / "config.toml"
            config.write_text(config.read_text().replace("[mcp_servers.fixture]",
                              f"[mcp_servers.{server}]").replace("timeout = 5", "timeout = 15"))
            if tool:
                mcp = tmp_path / "fixture_mcp.py"
                mcp.write_text(mcp.read_text().replace("def record()", f"def {tool}()"))
            (tmp_path / "hook.py").write_text(
                "import json,pathlib,subprocess,sys\npayload=json.load(sys.stdin)\n"
                f"with pathlib.Path({str(tmp_path / 'hooks.jsonl')!r}).open('a') as f: "
                "f.write(json.dumps(payload)+'\\n')\n"
                f"command={['/bin/bash', str(HOOKS / 'codex-validator-guard'), '--runtime-root', str(ROOT), '--workspace-root', str(tmp_path / 'workspace')]!r}\n"
                "try:\n"
                "    result=subprocess.run(command,input=json.dumps(payload).encode(),timeout=12)\n"
                "except subprocess.TimeoutExpired:\n"
                "    print('Fixture guard timed out',file=sys.stderr)\n"
                "    sys.exit(2)\n"
                "sys.exit(result.returncode)\n",
            )
            if case == "failed":
                fake_bin = tmp_path / "bin"
                fake_bin.mkdir()
                python = fake_bin / "python3"
                python.write_text("#!/bin/sh\nexit 0\n")
                python.chmod(0o755)
                env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]
            evidence["trust"] = trust_fixture_hook(binary, tmp_path, env)
            events, stderr = run_native(binary, client, tmp_path, env, evidence)
            evidence.update(events=events, stderr=stderr, requests=requests)
    finally:
        (tmp_path / "native-evidence.json").write_text(json.dumps(evidence, indent=2))
    hooks = [json.loads(line) for line in (tmp_path / "hooks.jsonl").read_text().splitlines()]
    assert len(hooks) == 1 and len(requests) == 2
    sink = case.removeprefix("code_") if tool is None else "mcp"
    assert hooks[0]["tool_name"] == ({"shell": "Bash", "patch": "apply_patch"}.get(sink)
                                    or f"mcp__{server}__{tool}")
    receipt = tmp_path / "workspace" / f"{sink}-receipt.txt"
    assert receipt.exists() == (case in {"health", "memory"})
