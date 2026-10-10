"""Run the installer's real code-copy and config-generation phases in scratch dirs."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("layout", ["existing", "fresh", "partial", "same_directory"])
def test_code_copy_and_generation_preserve_operator_config(tmp_path, layout):
    source = tmp_path / "repo"
    (source / "src/genesis/guardian").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "scripts/shipped.sh").write_text("# shipped\n")
    (source / "config/nested").mkdir(parents=True)
    (source / "config/nested/default.yaml").write_text("setting: shipped\n")
    (source / "config/guardian.yaml").write_text("container_name: shipped-template\n")
    install = source if layout == "same_directory" else tmp_path / "installed"
    if layout in ("existing", "partial", "same_directory"):
        (install / "config").mkdir(parents=True, exist_ok=True)
        (install / "config/guardian.yaml").write_text(
            '# operator note\ncontainer_name: operator-container\ncontainer_ip: "192.0.2.1"\n'
        )
    if layout == "existing":
        (install / "src/genesis/guardian").mkdir(parents=True)
    script = (_REPO / "scripts/install_guardian.sh").read_text()
    copy_phase = script.split("# If already running from the install dir", 1)[1].split(
        "# ── Step 3:", 1
    )[0]
    # Restore the first comment line whose prefix was the extraction delimiter.
    copy_phase = "# If already running from the install dir" + copy_phase
    generation = script.split("# ── Step 5:", 1)[1].split("# ── Step 6:", 1)[0]
    generation = generation[generation.index('echo ""') :]
    body = (
        """set -euo pipefail
REPO_ROOT=$1
INSTALL_DIR=$2
PYTHON=$3
CONTAINER_NAME=detected-container
CONTAINER_IP=192.0.2.2
HEALTH_PORT=5000
CC_ENABLED=false
CLAUDE_PATH=/fixture/claude
"""
        + copy_phase
        + generation
    )
    result = subprocess.run(
        ["bash", "-c", body, "installer-copy-test", str(source), str(install), sys.executable],
        cwd=_REPO,
        env={"PATH": os.defpath, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    content = (install / "config/guardian.yaml").read_text()
    settings = yaml.safe_load(content)
    if layout == "fresh":
        assert settings["container_name"] == "detected-container"
        assert settings["container_ip"] == "192.0.2.2"
    else:
        assert settings["container_name"] == "operator-container"
        assert settings["container_ip"] == "192.0.2.1"
        assert "# operator note" in content
    assert (install / "config/nested/default.yaml").read_text() == "setting: shipped\n"
    assert (install / "scripts/shipped.sh").read_text() == "# shipped\n"
