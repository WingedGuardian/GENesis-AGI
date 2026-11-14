"""The shipped service command must enter the actual captured runner role."""
import os
import shlex
import subprocess
import sys
from pathlib import Path

from tests.test_scripts.test_code_intel_source import case
from tests.test_scripts.test_code_intel_source import isolated_modules as isolated_modules


def test_service_exec_enters_runner_and_reaches_negative_readiness(tmp_path, monkeypatch):
    root, _, _ = case(tmp_path, monkeypatch, 'runner')
    path = Path(__file__).resolve().parents[2]/'scripts/systemd/genesis-code-intel.service.template'
    line, = [row.removeprefix('ExecStart=:') for row in path.read_text().splitlines()
             if row.startswith('ExecStart=:')]
    replacements = {'__VENV_EXEC__': str(Path(sys.executable).parent.parent), '__REPO_EXEC__': str(root)}
    for old, new in replacements.items():
        line = line.replace(old, new)
    result = subprocess.run(shlex.split(line), cwd='/', env=dict(os.environ), capture_output=True, text=True)
    assert result.returncode == 0, result.stdout+result.stderr
    log = tmp_path/'state/code-intelligence-runner.log'
    assert 'automatic queue protocol incomplete' in log.read_text()
    assert not (tmp_path/'state/index-requests/queue.sqlite3').exists()
