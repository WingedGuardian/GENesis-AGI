"""Pinned shell→adapter→filer composition; every GitHub sink is an owned fake."""

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
from tests.test_scripts.test_file_tracker_comments_e2e import prepare_owned_github

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1", reason="opt-in pinned composed filer qualification",
)


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("mode", ["scanned", "private", "raw_denied", "noop"])
def test_pinned_adapter_and_scanned_filer(tmp_path, monkeypatch, client, mode):
    binary = os.environ["GENESIS_CODEX_CLI_BIN"]
    assert subprocess.check_output([binary, "--version"], text=True).strip() == "codex-cli 0.163.0"
    assert hashlib.sha256(Path(binary).read_bytes()).digest() == bytes.fromhex(
        "399ff69c 431c066e 40de318f ba45c2b0 7f45a76b 19939d24 7ee4828f c70c4215"
    )
    # Local build-ahead may name the pending dependency. Final qualification
    # uses this repository after the predecessor has actually merged.
    dependency = Path(os.environ.get("GENESIS_VALIDATOR_HOOK_SOURCE", ROOT)).resolve()
    helper = private_module("filer_native_source_copy", dependency / "tests/test_hooks/test_codex_validator_adapter_native.py")
    monkeypatch.setattr(helper, "ROOT", dependency)
    runtime = helper.source_copy(tmp_path)
    monkeypatch.syspath_prepend(str(runtime / "scripts/hooks"))
    definition = private_module("filer_native_adapter", runtime / "scripts/hooks/codex_validator_adapter.py")
    for directory in ("contribution", "util"):
        shutil.copytree(ROOT / "src/genesis" / directory, runtime / "src/genesis" / directory,
                        ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copyfile(ROOT / "scripts/file_tracker_issue.py", runtime / "scripts/file_tracker_issue.py")
    sink_env, body, original = prepare_owned_github(
        tmp_path, "private_fingerprint" if mode == "private" else "clean_snapshot",
    )
    scratch = tmp_path / "tmp/validator"
    scratch.mkdir(parents=True)
    child_env = [f"{key}={value}" for key, value in sink_env.items()]
    # Put the fake PATH in the executed argv, not just the parent environment:
    # the client's login shell may reset the parent's PATH.
    argv = ["/usr/bin/env", *child_env]
    if mode in {"raw_denied", "noop"}:
        argv += [str(tmp_path / "bin/gh"), "pr", "comment", "7", "--body", "owned fixture"]
    else:
        argv += [sys.executable, str(runtime / "scripts/file_tracker_issue.py"),
                 "--comment-on", "7", "--body-file", str(body)]
    command = "cd " + shlex.quote(str(scratch)) + " || exit\n" + shlex.join(argv)
    item = {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
            "namespace": "functions", "name": "exec",
            "input": "text(await tools.exec_command(" + json.dumps({"cmd": command}) + "));"}
    hashes = {str(path.relative_to(runtime)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(runtime.rglob("*")) if path.is_file()}
    evidence = {"client": client, "mode": mode, "dependency": str(dependency),
                "runtime": str(runtime), "hashes": hashes, "command": command}
    try:
        with fixture_provider(item) as (url, requests):
            env = configure(tmp_path, url, "allow")
            env.update(HOME=str(tmp_path), GENESIS_HOME=str(tmp_path / ".genesis"),
                       GENESIS_CC_SESSION="1", _TEST_HOOK_ASK_POLICY="")
            config = tmp_path / "codex/config.toml"
            config.write_text(config.read_text().replace("timeout = 5", "timeout = 15"))
            invocation = "true" if mode == "noop" else definition.hook_command(runtime)
            (tmp_path / "hook.py").write_text(
                "import pathlib,subprocess,sys\nraw=sys.stdin.buffer.read()\n"
                f"with pathlib.Path({str(tmp_path / 'hooks.jsonl')!r}).open('a') as out: out.write(raw.decode()+'\\n')\n"
                f"result=subprocess.run(['bash','-c',{invocation!r}],input=raw)\n"
                "raise SystemExit(result.returncode)\n"
            )
            evidence["trust"] = trust_fixture_hook(binary, tmp_path, env)
            evidence["events"], evidence["stderr"] = run_native(binary, client, tmp_path, env, evidence)
            evidence["requests"] = requests
    finally:
        (tmp_path / "native-filer-evidence.json").write_text(json.dumps(evidence, indent=2))
    assert len(requests) == 2
    hooks = [json.loads(line) for line in (tmp_path / "hooks.jsonl").read_text().splitlines()]
    assert len(hooks) == 1 and hooks[0]["tool_name"] == "Bash"
    assert hooks[0]["tool_input"]["command"] == command
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()] if (tmp_path / "calls.jsonl").exists() else []
    if mode == "scanned":
        assert sum("POST" in args for args in calls) == 1, evidence
        assert json.loads((tmp_path / "posted.json").read_text())["body"].encode() == original
        assert body.read_bytes() != original
    elif mode == "private":
        assert calls == [] and "privacy scan" in json.dumps(requests[-1]), evidence
    elif mode == "raw_denied":
        assert calls == [] and "privacy-scanned filer" in json.dumps(requests[-1]), evidence
    else:
        assert json.loads((tmp_path / "raw-comment.json").read_text())[:2] == ["pr", "comment"]
        assert len(calls) == 1
    assert not (tmp_path / "codex/auth.json").exists()
    assert not list(Path(sink_env["HOME"]).glob("tmp/issue-body-*"))
    assert {str(p.relative_to(runtime)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(runtime.rglob("*")) if p.is_file() and "__pycache__" not in p.parts} == {
                key: value for key, value in hashes.items() if "__pycache__" not in Path(key).parts}
