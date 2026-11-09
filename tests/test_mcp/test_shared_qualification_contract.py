"""Real import boundaries and the separate CI qualification environment."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[2]
MODULES = ["test_shared_http_admission", "test_shared_http_policy", "test_shared_http_runtime",
           "test_shared_cli_startup_contract", "test_shared_forwarder", "test_shared_forwarder_http_errors",
           "test_shared_paths", "test_shared_path_tools", "test_shared_path_routes",
           "test_shared_path_versions", "test_shared_path_context"]
LAZY_SDK_MODULES = {"test_shared_path_context"}
PINS = {"fastmcp": "4.1.4", "mcp": "2.3.0", "uvicorn": "0.54.0"}

CHILD = """
import importlib,importlib.abc,json,sys
from importlib.metadata import PackageNotFoundError
from genesis.mcp.shared_http import _settings
versions=json.loads(sys.argv[1])
def version(name):
    if versions[name] is None: raise PackageNotFoundError(name)
    return versions[name]
_settings.version=version
seen=[]
class Sentinel(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name.split('.')[0] in {'mcp','mcp_types','fastmcp','starlette'}:
            seen.append(name)
            raise RuntimeError('SDK_IMPORT_SENTINEL')
assert not any(n.split('.')[0] in {'mcp','mcp_types','fastmcp','starlette'} for n in sys.modules)
sys.meta_path.insert(0,Sentinel())
try:
    importlib.import_module('tests.test_mcp.'+sys.argv[2])
except BaseException as error:
    print(json.dumps({'type':type(error).__name__,'error':str(error),'seen':seen}))
else: print(json.dumps({'type':'imported','error':None,'seen':seen}))
"""


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("strict", ["0", "1"])
@pytest.mark.parametrize("changed", [None, *(f"{name}:{kind}" for name in PINS for kind in ("missing", "later"))])
def test_qualification_precedes_any_sdk_import(module, strict, changed):
    versions = dict(PINS)
    if changed:
        name, kind = changed.split(":")
        versions[name] = None if kind == "missing" else PINS[name] + ".1"
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), GENESIS_REQUIRE_SHARED_MCP_QUALIFICATION=strict)
    run = subprocess.run([sys.executable, "-c", CHILD, json.dumps(versions), module], env=env,
                         cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    if changed is None:
        if module in LAZY_SDK_MODULES:
            assert result == {"type": "imported", "error": None, "seen": []}
        else:
            assert result["type"] == "RuntimeError" and result["error"] == "SDK_IMPORT_SENTINEL"
            assert len(result["seen"]) == 1
    else:
        assert result["seen"] == []
        assert result["type"] == ("RuntimeError" if strict == "1" else "Skipped"), result


def workflow():
    return yaml.safe_load((ROOT / ".github/workflows/shared-mcp-qualification.yml").read_text())


@pytest.mark.parametrize("mode", ["true", "", "2"])
def test_invalid_strict_mode_refuses_instead_of_skipping(mode):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"), GENESIS_REQUIRE_SHARED_MCP_QUALIFICATION=mode)
    run = subprocess.run([sys.executable, "-c", CHILD, json.dumps(PINS), MODULES[0]], env=env,
                         cwd=ROOT, capture_output=True, text=True, timeout=15)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout)
    assert result == {"type": "RuntimeError", "error": "Invalid shared MCP test qualification mode", "seen": []}


def test_pins_are_exact_complete_and_separate_from_ordinary_ci():
    path = ROOT / ".github/shared-mcp/requirements.txt"
    pins = [Requirement(line) for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    assert len(pins) == len({pin.name for pin in pins}) == 3
    assert {pin.name: str(pin.specifier) for pin in pins} == {
        name: "==" + version for name, version in PINS.items()}
    ordinary = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    installs = [step.get("run", "") for job in ordinary["jobs"].values()
                for step in job.get("steps", []) if "pip install -e" in step.get("run", "")]
    assert installs and all("-c .github/shared-mcp/requirements.txt" not in run for run in installs)
    tools = {Requirement(line).name for line in (ROOT / ".github/test-tools/requirements.txt").read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")}
    assert not set(PINS) & tools


def test_workflow_runs_stacked_prs_with_read_only_checkout():
    spec = workflow()
    # PyYAML's YAML1.1 loader reads the Actions `on` key as True.
    events = spec.get("on", spec.get(True))
    assert "branches" not in events["pull_request"]
    assert set(events["pull_request"]["types"]) >= {"opened", "synchronize", "edited", "reopened"}
    paths = events["pull_request"]["paths"]
    assert paths == events["push"]["paths"]
    assert set(paths) >= {"src/genesis/**", "tests/**", "scripts/**", "pyproject.toml",
        ".github/workflows/shared-mcp-qualification.yml", ".github/shared-mcp/**", ".github/test-tools/**"}
    assert spec["permissions"] == {"contents": "read"}
    steps = spec["jobs"]["qualify"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] is False


def test_protocol_step_is_strict_complete_and_has_no_skip_escape():
    job = workflow()["jobs"]["qualify"]
    assert str(job["env"]["GENESIS_REQUIRE_SHARED_MCP_QUALIFICATION"]) == "1"
    commands = [step.get("run", "") for step in job["steps"]]
    install = next(command for command in commands if "pip install -e" in command)
    assert "-c .github/shared-mcp/requirements.txt" in install
    assert "-c .github/test-tools/requirements.txt" in install
    protocol = next(command for command in commands if "--junitxml=junit-shared-mcp.xml" in command)
    for module in MODULES:
        assert f"tests/test_mcp/{module}.py" in protocol
    assert "tests/test_mcp/test_shared_path_policy.py" in protocol
    assert "tests/test_mcp/test_ordinary_preparation.py" in protocol
    assert "tests/test_mcp/test_plan_bookmark_consumption.py" in protocol
    assert protocol.index("rm -f junit-shared-mcp.xml") < protocol.index("-m pytest")
    check = next(command for command in commands if "scripts/check_skip_ceiling.py" in command)
    assert "junit-shared-mcp.xml --ceiling 0 --min-collected 422" in check
    assert not job.get("continue-on-error")
    assert not any(step.get("continue-on-error") for step in job["steps"])
