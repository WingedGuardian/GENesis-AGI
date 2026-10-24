"""The opt-in unit must preserve lock inodes owned by a manual local host."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def test_runtime_directory_survives_failed_unit_start():
    template = Path(__file__).resolve().parents[2] / "scripts/systemd/genesis-live-agent.service.template"
    text = template.read_text()
    assert "RuntimeDirectoryPreserve=yes" in text
    assert "RuntimeDirectoryMode=0700" in text
    assert "UMask=0077" in text
    assert "KillMode=mixed" in text


@pytest.mark.parametrize("directory", ["plain", "space dir", "percent%d", 'quote"dir', r"back\dir", "dollar$UID"])
def test_executable_path_survives_systemd_parsing(tmp_path, directory):
    venv = tmp_path / directory
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    template = Path(__file__).resolve().parents[2] / "scripts/systemd/genesis-live-agent.service.template"
    line = next(line for line in template.read_text().splitlines() if line.startswith("ExecStart="))
    escaped = str(venv).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    line = line.replace("__VENV__", str(venv)).replace("__VENV_EXEC__", escaped)
    assert line == f'''ExecStart=:/bin/sh -c 'exec "$@"' -- "{escaped}/bin/python" -m genesis.live_agent'''
    unit = tmp_path / "probe.service"
    unit.write_text("[Service]\nType=oneshot\n" + line + "\n")
    # Verification parses the command but never starts the unit. Keep its
    # unit search/runtime state in scratch; no manager reload or activation.
    env = {**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)}
    proc = subprocess.run(["systemd-analyze", "--user", "verify", str(unit)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    # Check the reused fixed shell program separately; this does not claim
    # that systemd activated the rendered unit or ran Genesis.
    literal = 'literal $UID %d " \\ value'
    proc = subprocess.run(
        ["/bin/sh", "-c", 'exec "$@"', "--", str(venv / "bin/python"), "-c",
         "import json,sys; print(json.dumps([sys.executable,sys.argv[1]]))", literal],
        env=env, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == [str(venv / "bin/python"), literal]


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
@pytest.mark.parametrize("suffix", ["plain", 'space quote"back\\slash', "percent%$&|λ"])
def test_both_real_renderers_escape_host_executable(tmp_path, renderer, suffix):
    from tests.test_scripts.test_codebase_managed_templates import ROOT, loop, prelude

    templates, units = tmp_path / "templates", tmp_path / "units"
    templates.mkdir()
    units.mkdir()
    name = "genesis-live-agent.service"
    shutil.copy2(ROOT / "scripts/systemd" / (name + ".template"), templates)
    home = tmp_path / suffix
    venv = home / ".venv"
    ccbin = home / "ccbin"
    env = {
        **os.environ, "HOME": str(home), "GENESIS_HOME": str(tmp_path / "state"),
        "GENESIS_DB_PATH": str(tmp_path / "absent.db"), "SECRETS_PATH": str(tmp_path / "absent.env"),
        "SYSTEMD_TEMPLATE_DIR": str(templates), "SYSTEMD_USER_DIR": str(units),
        "REPO_DIR": str(home), "GENESIS_ROOT": str(home), "VENV_PATH": str(venv),
        "CC_BIN_DIR": str(ccbin), "SERVICES_GENERATED": "0", "SERVICES_UPDATED": "0",
        "FALKORDB_VERSION": "4.20.4", "GENESIS_UPDATE_TIER": "0", "GENESIS_TAKE_TEMPLATES": "",
        "GENESIS_DEPLOY_SUMMARY": str(tmp_path / "summary"),
    }
    script = '_falkordb_redis_server_bin() { printf /usr/bin/redis-server; }\nsystemctl() { :; }\n'
    proc = subprocess.run(["/bin/bash", "-euc", script + prelude(tmp_path) + loop(renderer)], env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    body = (units / name).read_text()

    def escaped(path):
        return str(path).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")

    assert f'''ExecStart=:/bin/sh -c 'exec "$@"' -- "{escaped(venv)}/bin/python" -m genesis.live_agent''' in body
    assert "__" not in body
    assert "RuntimeDirectoryPreserve=yes" in body


def test_complete_rendered_unit_passes_systemd_parser(tmp_path):
    repo, venv, home = tmp_path / "repo", tmp_path / "venv", tmp_path / "home"
    repo.mkdir()
    home.mkdir()
    (venv / "bin").mkdir(parents=True)
    (venv / "bin/python").symlink_to(sys.executable)
    (repo / "secrets.env").touch()
    template = Path(__file__).resolve().parents[2] / "scripts/systemd/genesis-live-agent.service.template"
    body = template.read_text()
    values = {
        "__REPO_DIR__": str(repo), "__VENV__": str(venv),
        "__VENV_EXEC__": str(venv), "__HOME__": str(home), "__CC_BIN_DIR__": "/usr/bin",
    }
    for token, value in values.items():
        body = body.replace(token, value)
    assert "__" not in body
    unit = tmp_path / "full-probe.service"
    unit.write_text(body)
    proc = subprocess.run(
        ["systemd-analyze", "--user", "verify", str(unit)],
        env={**os.environ, "XDG_RUNTIME_DIR": str(tmp_path)}, capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
