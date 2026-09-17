"""Disk hygiene must propagate critical reclaim deferral after finishing its other steps."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_HYGIENE = _ROOT / "scripts" / "disk_hygiene.sh"


def test_disk_hygiene_returns_disk_reclaim_failure_after_best_effort_steps(tmp_path):
    fake_python = tmp_path / "python"
    fake_python.write_text(
        "#!/usr/bin/env bash\n"
        "case \"${1:-}\" in *disk_reclaim.py) exit 2 ;; *) exit 0 ;; esac\n"
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    repo = tmp_path / "repo"
    repo.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    command = (
        f'source "{_HYGIENE}"; '
        f'VENV_PY="{fake_python}"; REPO_DIR="{repo}"; HOME="{home}"; main'
    )

    result = subprocess.run(
        ["bash", "-c", command],
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert "disk_reclaim exited 2" in result.stdout
    assert "genesis-disk-hygiene done" in result.stdout
    assert result.returncode == 2
