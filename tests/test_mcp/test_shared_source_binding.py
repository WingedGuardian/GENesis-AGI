"""Actual script/import roots bind before secrets; a removed guard is detected."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("case", ["same-root", "foreign-script", "removed-early-guard"])
def test_launcher_binds_checkout_before_loading_secrets(tmp_path, case):
    root = Path(__file__).resolve().parents[2]
    actual = root / "scripts/genesis_mcp_server.py"
    injection = tmp_path / "injection"
    injection.mkdir()
    secret = tmp_path / "synthetic.env"
    secret.write_text("")
    marker = tmp_path / "secret-touched"
    (injection / "sitecustomize.py").write_text(
        "import os,sys\n"
        "def audit(event,args):\n"
        "    if event=='open' and args[0]==os.environ['SECRETS_PATH']:\n"
        "        fd=os.open(os.environ['SOURCE_BINDING_MARKER'],os.O_CREAT|os.O_WRONLY,0o600)\n"
        "        os.close(fd)\n"
        "        raise RuntimeError('synthetic secrets access marker')\n"
        "sys.addaudithook(audit)\n"
    )
    script = actual
    if case != "same-root":
        script = tmp_path / "foreign/scripts/genesis_mcp_server.py"
        script.parent.mkdir(parents=True)
        source = actual.read_text()
        if case == "removed-early-guard":
            guard = "shared_root = require_source_checkout(Path(__file__).resolve().parents[1])"
            assert source.count(guard) == 1
            source = source.replace(guard, "shared_root = Path(__file__).resolve().parents[1]")
        script.write_text(source)
    env = dict(os.environ, HOME=str(tmp_path), GENESIS_HOME=str(tmp_path),
        GENESIS_REPO_ROOT=str(root), GENESIS_DB_PATH=str(tmp_path / "unused.db"),
        SECRETS_PATH=str(secret), SOURCE_BINDING_MARKER=str(marker),
        GENESIS_MCP_ENV_RECORDER="0", PYTHONPATH=os.pathsep.join([str(injection), str(root / "src")]))
    run = subprocess.run([sys.executable, str(script), "--shared-service", "health",
        "--transport", "streamable-http"], env=env, capture_output=True, text=True, timeout=15)
    assert run.returncode != 0
    if case == "foreign-script":
        assert "launcher and imported source differ" in run.stderr, run.stderr
        assert not marker.exists()
    else:
        assert marker.exists() and "synthetic secrets access marker" in run.stderr, run.stderr
