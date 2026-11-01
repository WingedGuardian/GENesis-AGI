"""Pinned Codex dispatch to owned real Genesis tools; no model or live stores."""

import hashlib
import json
import os
import shutil
import sqlite3
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
from tests.test_mcp.validator_stdio_fixture import prepare
from tests.test_scripts.test_pr_verification_closer import _doc

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1",
                              reason="opt-in pinned native writer qualification")
CASES = ["private", "preview", "record", "episodic_memory", "knowledge_base", "external_health", "external_memory"]


def decoded_tool_result(result):
    assert not result.get("isError") and "error" not in result, result
    if result.get("structuredContent") is not None:
        value = result["structuredContent"]
        return value["result"] if isinstance(value, dict) and set(value) == {"result"} else value
    assert len(result["content"]) == 1 and result["content"][0]["type"] == "text", result
    text = result["content"][0]["text"]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("case", CASES)
async def test_native_validator_writer_dispatch(tmp_path, monkeypatch, client, case):
    binary = os.environ["GENESIS_CODEX_CLI_BIN" if client == "cli" else "GENESIS_CODEX_APP_SERVER_BIN"]
    app = os.environ["GENESIS_CODEX_APP_SERVER_BIN"]
    assert subprocess.check_output([binary, "--version"], text=True).strip() == "codex-cli 0.163.0"
    expected_binary = bytes.fromhex("399ff69c 431c066e 40de318f ba45c2b0 7f45a76b 19939d24 7ee4828f c70c4215")
    assert hashlib.sha256(Path(binary).read_bytes()).digest() == expected_binary
    assert hashlib.sha256(Path(app).read_bytes()).digest() == expected_binary
    profile = "external" if case.startswith("external_") else "validator"
    fixture = await prepare(tmp_path, profile=profile, recording=case == "record")
    memory = case in {"episodic_memory", "knowledge_base", "external_memory"}
    server = "memory" if memory else "health"
    tool = "memory_store" if memory else "pr_verification_close" if case in {"preview", "record"} else "follow_up_create"
    arguments = ({"content": "Owner-directed synthetic native measurement", "source": "native fixture",
                  "confidence": 0.95, "memory_class": "fact", "wing": "dev_workflow", "room": "validation",
                  "collection": case if memory and profile == "validator" else "episodic_memory"}
                 if memory else {"pr": 7, "evidence_json": json.dumps(_doc())}
                 if case in {"preview", "record"} else
                 {"content": "Synthetic native private finding", "reason": "Synthetic native evidence",
                  "work_state": "ready", "strategy": "user_input_needed", "request_key": "native-finding"})
    key = "mcp__genesis_validator_" + server + "__" + tool
    program = ('const key=' + json.dumps(key) + '; const catalog=ALL_TOOLS.filter(t=>'
               't.name.startsWith("mcp__genesis_validator_")).map(t=>t.name).sort(); '
               'let result; try {result=await tools[key](' + json.dumps(arguments) + ');}'
               'catch(error){result={error:String(error)};} '
               'text("VALIDATOR_WRITER:"+JSON.stringify({catalog,result}));')
    item = {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
            "namespace": "functions", "name": "exec", "input": program}
    adapter = Path(os.environ["GENESIS_VALIDATOR_ADAPTER_SOURCE"]).resolve()
    runtime = tmp_path / "adapter-runtime"
    for directory in ("scripts/hooks", ".claude/hooks"):
        shutil.copytree(adapter / directory, runtime / directory, ignore=shutil.ignore_patterns("__pycache__"))
    (runtime / ".venv").symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
    for relative in ("src/genesis/__init__.py", "src/genesis/eval/__init__.py",
                     "src/genesis/eval/qualification/__init__.py", "src/genesis/eval/qualification/evidence.py"):
        target = runtime / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(adapter / relative, target)
    monkeypatch.syspath_prepend(str(runtime / "scripts/hooks"))
    definition = private_module("native_writer_adapter", runtime / "scripts/hooks/codex_validator_adapter.py")
    sources = {"writer/" + str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in [ROOT / "scripts/genesis_mcp_server.py", Path(__file__),
                         ROOT / "tests/test_mcp/validator_stdio_fixture.py", ROOT / "tests/test_hooks/native_codex.py",
                         *sorted((ROOT / "src/genesis").rglob("*.py"))]}
    sources.update({"adapter/" + str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in runtime.rglob("*") if p.is_file() and ".venv" not in p.parts})
    evidence = {"client": client, "case": case, "profile": profile, "source_root": str(ROOT),
                "adapter_source": str(adapter), "adapter_runtime": str(runtime), "hashes": sources,
                "database": str(fixture["database"]), "integration_preview": True,
                "owned_recording_enabled": case == "record"}
    try:
        with fixture_provider(item) as (url, requests):
            env = configure(tmp_path, url, "allow") | fixture["environment"]
            config = tmp_path / "codex/config.toml"
            config.write_text(config.read_text().split("[mcp_servers.fixture]")[0].replace("timeout = 5", "timeout = 15"))
            with config.open("a") as output:
                for name, entry in fixture["servers"].items():
                    output.write("\n[mcp_servers.genesis_validator_" + name + "]\n")
                    output.write("command=" + json.dumps(entry["command"]) + "\nargs=" + json.dumps(entry["args"]) + "\n")
                    output.write('required=true\nstartup_timeout_sec=30\ndefault_tools_approval_mode="approve"\n')
            hook = tmp_path / "hook.py"
            invocation = ["bash", "-c", definition.hook_command(runtime)]
            hook.write_text('import pathlib,subprocess,sys\nraw=sys.stdin.buffer.read()\n'
                            f'with pathlib.Path({str(tmp_path / "hooks.jsonl")!r}).open("a") as out: out.write(raw.decode()+"\\n")\n'
                            f'raise SystemExit(subprocess.run({invocation!r},input=raw).returncode)\n')
            evidence["trust"] = trust_fixture_hook(app, tmp_path, env)
            evidence["events"], evidence["stderr"] = run_native(binary, client, tmp_path, env, evidence)
            evidence["requests"] = requests
        assert len(requests) == 2
        outputs = [block["text"] for row in requests[1]["input"]
                   if row.get("type") == "custom_tool_call_output" and row.get("call_id") == "call_fixture"
                   for block in row["output"] if block.get("type") == "input_text"]
        records = [json.loads(line.removeprefix("VALIDATOR_WRITER:")) for output in outputs
                   for line in output.splitlines() if line.startswith("VALIDATOR_WRITER:")]
        assert len(records) == 1, outputs
        record = records[0]
        health = [name for name in record["catalog"] if name.startswith("mcp__genesis_validator_health__")]
        memories = [name for name in record["catalog"] if name.startswith("mcp__genesis_validator_memory__")]
        assert (len(health), len(memories)) == ((5, 8) if profile == "external" else (9, 9))
        hooks = [json.loads(line) for line in (tmp_path / "hooks.jsonl").read_text().splitlines()] if (tmp_path / "hooks.jsonl").exists() else []
        if profile == "external":
            assert key not in record["catalog"] and "not a function" in record["result"]["error"]
            assert not hooks
        else:
            assert key in record["catalog"]
            returned = decoded_tool_result(record["result"])
            assert len(hooks) == 1 and hooks[0]["tool_name"] == key
            if case in {"preview", "record"}:
                assert returned["dry_run"] is (case == "preview")
                assert returned["server_dry_run_only"] is (case == "preview")
                assert returned["recorded"] is (case == "record")
                assert returned["would_close"] is True and returned["pr"] == 7 and returned["repo"] == "owner/repo"
                assert returned["verdict"] == "pass-mechanical"
        with sqlite3.connect(fixture["database"]) as reader:
            assert reader.execute("SELECT COUNT(*) FROM follow_ups").fetchone()[0] == int(case == "private")
            assert reader.execute("SELECT status,attempt_count FROM pr_verifications").fetchall() == [
                ("closed" if case == "record" else "open", int(case == "record"))]
            assert reader.execute("SELECT COUNT(*) FROM memory_fts").fetchone()[0] == int(memory and profile == "validator")
            if memory and profile == "validator":
                assert reader.execute("SELECT collection FROM memory_fts").fetchall() == [(case,)]
                assert reader.execute("SELECT memory_id FROM memory_metadata").fetchall() == [(returned,)]
                vectors = json.loads((tmp_path / "vectors.json").read_text())
                assert len(vectors) == 1 and vectors[0]["id"] == returned
            if case == "private":
                assert reader.execute("SELECT id, source, source_session FROM follow_ups").fetchall() == [
                    (returned["id"], "codex_interactive", None)]
            if profile == "validator":
                assert reader.execute("SELECT COUNT(*) FROM activity_log").fetchone()[0] == 0
    finally:
        (tmp_path / "native-writer-evidence.json").write_text(json.dumps(evidence, indent=2))
