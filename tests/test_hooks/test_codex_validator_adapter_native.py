"""Pinned native qualification, isolated source copies and harmless actual sinks."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module
from tests.test_hooks.native_codex import (
    configure,
    fixture_provider,
    run_native,
    trust_fixture_hook,
)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1", reason="opt-in native qualification")


def source_copy(root):
    runtime = root / "runtime"
    for directory in ("scripts/hooks", ".claude/hooks"):
        shutil.copytree(ROOT / directory, runtime / directory, ignore=shutil.ignore_patterns("__pycache__"))
    # genesis-hook deliberately refuses an uninstalled checkout. Reuse only the
    # existing interpreter/dependencies; executable source remains the copy.
    (runtime / ".venv").symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
    for relative in ("src/genesis/__init__.py", "src/genesis/eval/__init__.py",
                     "src/genesis/eval/qualification/__init__.py", "src/genesis/eval/qualification/evidence.py"):
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return runtime


ALLOW_CASES = ["shell", "patch", "code_shell", "code_patch", "mcp", "mcp_direct", "advisory", "allow-reason", "patch-relative", "outer-timeout"]
DENY_CASES = ["deny", "ask", "crash", "missing", "malformed", "timeout", "patch-deny", "missing-launcher", "broken-launcher", "silent-launcher", "mcp_direct-deny", "mcp-deny", "outer-completes"]


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("case,instrument", [(c, "adapter") for c in ALLOW_CASES + DENY_CASES] + [(c, "noop") for c in DENY_CASES])
def test_native_adapter_controls_sink(tmp_path, monkeypatch, client, case, instrument):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/hooks"))
    definition = private_module("native_adapter_definition", ROOT / "scripts/hooks/codex_validator_adapter.py")
    cli = os.environ.get("GENESIS_CODEX_CLI_BIN") or shutil.which("codex")
    app = os.environ.get("GENESIS_CODEX_APP_SERVER_BIN") or cli
    binary = cli if client == "cli" else app
    assert binary
    version = subprocess.check_output([binary, "--version"], text=True, timeout=5).strip()
    assert version == "codex-cli 0.163.0"
    runtime = source_copy(tmp_path)
    scratch = tmp_path / "tmp/validator"
    scratch.mkdir(parents=True)
    run_root = scratch / "session" if case == "patch-relative" else tmp_path
    run_root.mkdir(exist_ok=True)
    sink = "patch" if "patch" in case else "mcp" if case.startswith("mcp") else "shell"
    receipt = (tmp_path / "workspace" if case == "patch-deny" or sink == "mcp" else scratch) / f"{sink}-receipt.txt"
    if case == "patch-relative":
        receipt = run_root / "workspace/patch-receipt.txt"
    command = f"cd {scratch} && printf fixture > {receipt}"
    body_marker = "fixture-body: xargs git worktree remove"
    if case == "shell":
        body = scratch / "fixture-body.txt"
        body.write_text(body_marker + "\n")
        command = f"cd {scratch} && cat {shlex.quote(str(body))}; printf fixture > {receipt}"
    if case == "advisory":
        command = f"cd {scratch} && false | true; echo $?; printf fixture > {receipt}"
    if case == "deny":
        # Harmless even if unfenced: scratch is not a git repo, and only a local
        # receipt follows the failed reset. No real service or production write.
        command = f"cd {scratch} && git reset --hard; printf fixture > {receipt}"
    patch_name = receipt.name if case == "patch-relative" else str(receipt)
    patch = f"*** Begin Patch\n*** Add File: {patch_name}\n+fixture\n*** End Patch"
    if case.startswith("mcp_direct"):
        item = {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture", "name": "mcp__genesis_validator_memory__record", "arguments": "{}"}
    elif case in {"patch", "patch-deny", "patch-relative"}:
        item = {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture", "name": "apply_patch", "input": patch}
    elif case in {"code_shell", "code_patch", "mcp", "mcp-deny"}:
        program = {
            "code_shell": f"text(await tools.exec_command({json.dumps({'cmd': command})}));",
            "code_patch": f"text(await tools.apply_patch({json.dumps(patch)}));",
            "mcp": "text(await tools.mcp__genesis_validator_memory__record({}));",
            "mcp-deny": "text(await tools.mcp__genesis_validator_memory__record({}));",
        }[case]
        item = {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture", "namespace": "functions", "name": "exec", "input": program}
    else:
        item = {"type": "function_call", "id": "fc_fixture", "call_id": "call_fixture", "name": "exec_command", "arguments": json.dumps({"cmd": command, "workdir": str(tmp_path)})}
    guard = runtime / "scripts/hooks" / ("secrets_env_access_guard.py" if case in {"mcp_direct-deny", "mcp-deny"} else "pipe_status_guard.py")
    fault = {"ask": 'print(\'{"hookSpecificOutput":{"permissionDecision":"ask","permissionDecisionReason":"fixture owner approval"}}\')',
             "allow-reason": 'print(\'{"hookSpecificOutput":{"permissionDecision":"allow","permissionDecisionReason":"fixture successful reason"}}\')',
             "crash": "raise RuntimeError('fixture')", "malformed": "print('not-json')",
             "timeout": "import time; time.sleep(30)"}
    fault["mcp_direct-deny"] = fault["ask"]
    fault["mcp-deny"] = fault["ask"]
    if case in fault:
        guard.write_text(fault[case])
    elif case == "missing":
        guard.unlink()
    wrapper = runtime / "scripts/hooks/codex-validator-adapter"
    if case == "missing-launcher":
        wrapper.unlink()
    elif case in {"broken-launcher", "silent-launcher"}:
        wrapper.write_text("if broken" if case == "broken-launcher" else "exit 0")
    elif case in {"outer-timeout", "outer-completes"}:
        # Identical delayed DENY: the short deadline permits, the normal
        # deadline refuses. CLI JSON omits hook timeout diagnostics. No
        # adapter-owned children are spawned by this residual/oracle pair.
        wrapper.write_text(f"printf started > {shlex.quote(str(tmp_path / 'outer-started'))}\n"
                           "sleep 2\nprintf deny >&3\necho fixture-late-denial >&2\nexit 2\n")
    evidence = {"client": client, "case": case, "instrument": instrument, "version": version,
                "runtime": str(runtime), "receipt": str(receipt),
                "hashes": {str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in sorted(runtime.rglob("*")) if p.is_file()}}
    try:
        with fixture_provider(item) as (url, requests):
            env = configure(run_root, url, "allow")
            env["HOME"] = str(tmp_path)
            env["GENESIS_HOME"] = str(tmp_path / ".genesis")
            env["GENESIS_CC_SESSION"] = "1"  # Adapter must scrub this for children.
            env["_TEST_HOOK_ASK_POLICY"] = ""
            config = run_root / "codex/config.toml"
            config.write_text(config.read_text().replace("timeout = 5", "timeout = 1" if case == "outer-timeout" else "timeout = 15").replace("mcp_servers.fixture", "mcp_servers.genesis_validator_memory"))
            # Keep the original native hook-input log, then pass exactly those
            # bytes to the copied adapter. No source under test is monkeypatched
            # except the explicit per-case broken child above.
            hook = run_root / "hook.py"
            hook_command = definition.hook_command(runtime) if instrument == "adapter" else "true"
            hook.write_text("import json,pathlib,subprocess,sys\nraw=sys.stdin.buffer.read()\n"
                f"with pathlib.Path({str(run_root / 'hooks.jsonl')!r}).open('a') as f: f.write(raw.decode()+'\\n')\n"
                f"r=subprocess.run(['bash','-c',{hook_command!r}],input=raw)\n"
                "raise SystemExit(r.returncode)\n")
            evidence["trust"] = trust_fixture_hook(app, run_root, env)
            evidence["events"], evidence["stderr"] = run_native(binary, client, run_root, env, evidence)
            evidence["requests"] = requests
    finally:
        (tmp_path / "native-evidence.json").write_text(json.dumps(evidence, indent=2))
    assert len(requests) == 2
    if case.startswith("mcp_direct"):
        # Raw direct MCP function calls are not exposed by this model's native
        # tool metadata and are rejected by the client before any hook, even
        # with a no-op instrument. This is NOT adapter denial qualification.
        assert not (run_root / "hooks.jsonl").exists() and not receipt.exists()
        assert "unsupported call: mcp__genesis_validator_memory__record" in json.dumps(requests[-1])
        return
    hook_inputs = [json.loads(line) for line in (run_root / "hooks.jsonl").read_text().splitlines()]
    assert len(hook_inputs) == 1
    assert hook_inputs[0]["tool_name"] == {"shell": "Bash", "patch": "apply_patch", "mcp": "mcp__genesis_validator_memory__record"}[sink]
    if case in {"outer-timeout", "outer-completes"} and instrument == "adapter":
        assert (tmp_path / "outer-started").read_text() == "started"
        effective = evidence["trust"]["after"]["data"][0]["hooks"]
        assert len(effective) == 1 and effective[0]["trustStatus"] == "trusted"
        assert effective[0]["timeoutSec"] == (1 if case == "outer-timeout" else 15)
    allowed = instrument == "noop" or case in ALLOW_CASES
    assert receipt.exists() == allowed, evidence
    if allowed:
        assert receipt.read_text().strip() == "fixture"
        if case == "advisory":
            assert "PIPESTATUS" not in json.dumps(requests[0]["input"])
            assert "PIPESTATUS" in json.dumps(requests[1]["input"])
        if case == "allow-reason":
            assert "fixture successful reason" not in json.dumps(requests[0]["input"])
            assert "fixture successful reason" in json.dumps(requests[1]["input"])
        if case == "shell":
            assert body_marker not in json.dumps(requests[0]["input"])
            assert body_marker in json.dumps(requests[1]["input"])
    else:
        # Refusal must come from the intended boundary, never an unrelated broken
        # fixture that merely happened to suppress every receipt.
        marker = {"ask": "fixture owner approval", "crash": "guard failed",
                  "missing": "guard failed", "malformed": "guard failed",
                  "timeout": "guard timed out", "patch-deny": "scratch roots",
                  "missing-launcher": "hook did not explicitly allow",
                  "broken-launcher": "hook did not explicitly allow",
                  "silent-launcher": "hook did not explicitly allow",
                  "deny": "destroys uncommitted work", "mcp_direct-deny": "fixture owner approval", "mcp-deny": "fixture owner approval",
                  "outer-completes": "fixture-late-denial"}[case]
        assert marker in json.dumps(evidence)
