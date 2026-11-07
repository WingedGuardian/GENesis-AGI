"""Real pinned clients and shared guards; every executable push sink is owned."""

import hashlib
import json
import os
import shlex
import shutil
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
from tests.test_hooks.test_codex_native import native_binaries as native_binaries

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1", reason="opt-in pinned shared-guard qualification",
)


@pytest.mark.parametrize("client", ["cli", "app"])
@pytest.mark.parametrize("prefix", [r"echo It\'s", r'echo q\"r'])
@pytest.mark.parametrize("mode", ["read", "deny", "noop"])
def test_pinned_shared_guard_sees_hash_free_escaped_word(tmp_path, monkeypatch, native_binaries, client, prefix, mode):
    binaries, versions = native_binaries
    assert set(versions.values()) == {"codex-cli 0.166.0"}
    binary_hashes = {name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                     for name, path in binaries.items()}
    expected_binary = bytes.fromhex("b8a11fed f24b2dfb 59a70c9e 52b030f9 a508dcd4 9cd2dd0d 41956f4c 785156b8").hex()
    assert set(binary_hashes.values()) == {expected_binary}
    binary = binaries[client]
    dependency = Path(os.environ.get("GENESIS_VALIDATOR_ADAPTER_SOURCE", ROOT)).resolve()
    helpers = private_module("quote_native_helpers", dependency / "tests/test_hooks/test_codex_validator_adapter_native.py")
    monkeypatch.setattr(helpers, "ROOT", ROOT)
    runtime = helpers.source_copy(tmp_path)
    # Pending PR1 supplies only this inactive adapter. Shared guard/parser source
    # always comes from the quote candidate, and every byte is retained below.
    overlays = ["scripts/hooks/codex-validator-adapter", "scripts/hooks/codex_validator_adapter.py"]
    for relative in overlays:
        shutil.copy2(dependency / relative, runtime / relative)
    monkeypatch.syspath_prepend(str(runtime / "scripts/hooks"))
    definition = private_module("quote_native_definition", runtime / "scripts/hooks/codex_validator_adapter.py")
    scratch = tmp_path / "tmp/validator"
    scratch.mkdir(parents=True)
    receipt = scratch / "git-receipt.json"
    fake_git = tmp_path / "bin/git"
    fake_git.parent.mkdir()
    fake_git.write_text(
        f"#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n"
        f"Path({str(receipt)!r}).write_text(json.dumps(sys.argv[1:]))\n"
    )
    fake_git.chmod(0o700)
    verb = "status" if mode == "read" else "push"
    command = "cd " + shlex.quote(str(scratch)) + " || exit\n" + prefix + "; "
    command += shlex.join([str(fake_git), verb])
    item = {"type": "custom_tool_call", "id": "ct_fixture", "call_id": "call_fixture",
            "namespace": "functions", "name": "exec",
            "input": "text(await tools.exec_command(" + json.dumps({"cmd": command}) + "));"}
    hashes = {str(path.relative_to(runtime)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted(runtime.rglob("*")) if path.is_file()}
    evidence = {"client": client, "versions": versions, "binary_hashes": binary_hashes,
                "prefix": prefix, "mode": mode, "command": command,
                "runtime": str(runtime), "hashes": hashes, "adapter_source": str(dependency),
                "adapter_overlays": overlays}
    try:
        with fixture_provider(item) as (url, requests):
            env = configure(tmp_path, url, "allow")
            env.update(HOME=str(tmp_path), GENESIS_HOME=str(tmp_path / ".genesis"),
                       GENESIS_CC_SESSION="1", _TEST_HOOK_ASK_POLICY="")
            config = tmp_path / "codex/config.toml"
            config.write_text(config.read_text().replace("timeout = 5", "timeout = 15"))
            hook = "true" if mode == "noop" else definition.hook_command(runtime)
            (tmp_path / "hook.py").write_text(
                "import pathlib,subprocess,sys\nraw=sys.stdin.buffer.read()\n"
                f"with pathlib.Path({str(tmp_path / 'hooks.jsonl')!r}).open('a') as out: out.write(raw.decode()+'\\n')\n"
                f"result=subprocess.run(['bash','-c',{hook!r}],input=raw)\n"
                "raise SystemExit(result.returncode)\n"
            )
            evidence["trust"] = trust_fixture_hook(binaries["app"], tmp_path, env)
            evidence["events"], evidence["stderr"] = run_native(binary, client, tmp_path, env, evidence)
            evidence["requests"] = requests
    finally:
        (tmp_path / "native-quote-evidence.json").write_text(json.dumps(evidence, indent=2))
    assert len(requests) == 2
    inputs = [json.loads(row) for row in (tmp_path / "hooks.jsonl").read_text().splitlines()]
    assert len(inputs) == 1 and inputs[0]["tool_name"] == "Bash"
    assert inputs[0]["tool_input"]["command"] == command
    if mode == "deny":
        assert not receipt.exists(), evidence
        assert "this push carries a prefix" in json.dumps(requests[-1]), evidence
    else:
        assert json.loads(receipt.read_text()) == [verb], evidence
    assert not (tmp_path / "codex/auth.json").exists()
