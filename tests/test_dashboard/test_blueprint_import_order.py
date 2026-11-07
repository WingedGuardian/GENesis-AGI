"""Selected dashboard tests must see the same routes as production."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "first_import",
    [
        "genesis.dashboard.routes.backup",
        "genesis.dashboard.api",
        "genesis.dashboard.routes.autonomy",
    ],
)
def test_dashboard_bootstrap_completes_routes_before_registration(tmp_path, first_import):
    # A fresh interpreter is essential: suite collection can otherwise import
    # api elsewhere and conceal the partial-import failure this pins.
    repo = Path(__file__).resolve().parents[2]
    script = """
import importlib
import runpy
import sys
from flask import Flask

bootstrap = runpy.run_path(sys.argv[1])
importlib.import_module(sys.argv[2])
bootstrap['pytest_runtest_setup']()
from genesis.dashboard._blueprint import blueprint
app = Flask(__name__)
app.config['TESTING'] = True
app.register_blueprint(blueprint)
api = importlib.import_module('genesis.dashboard.api')
from pathlib import Path
assert Path(api.__file__).resolve().is_relative_to(Path.cwd() / 'src')
client = app.test_client()
assert client.get('/genesis').status_code == 200
assert client.get('/api/genesis/auth/status').status_code == 200
print('canonical dashboard routes available after registration')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo / "tests/conftest.py"), first_import],
        cwd=repo,
        env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": str(repo / "src")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "canonical dashboard routes available after registration"


def test_unrelated_test_setup_does_not_import_dashboard(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    # If dashboard imports leak into unrelated setup, its MCP FileHandler would
    # fail here. This is a scratch HOME; no real log or credential is accessed.
    (tmp_path / "tmp/mcp_health.log").mkdir(parents=True)
    script = """
import runpy
import sys
bootstrap = runpy.run_path(sys.argv[1])
assert 'genesis.dashboard._blueprint' not in sys.modules
bootstrap['pytest_runtest_setup']()
assert 'genesis.dashboard.api' not in sys.modules
print('unrelated setup leaves dashboard unloaded')
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(repo / "tests/conftest.py")],
        cwd=repo,
        env={"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": str(repo / "src")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "unrelated setup leaves dashboard unloaded"
