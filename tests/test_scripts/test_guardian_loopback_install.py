"""Run the real installer config phase against preserved scratch settings."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "listener,configured,expected",
    [
        ("tcp:127.0.0.1:5000", "", "127.0.0.1"),
        ("tcp:127.0.0.1:5000", "localhost", "localhost"),
        ("tcp:0.0.0.0:5000", "", ""),
    ],
)
def test_installer_aligns_unset_http_target_without_clobbering_config(
    tmp_path, listener, configured, expected
):
    install = tmp_path / "guardian"
    (install / "config").mkdir(parents=True)
    (install / "src").symlink_to(_REPO / "src", target_is_directory=True)
    config = install / "config/guardian.yaml"
    config.write_text(
        f'# retain operator note\ncontainer_ip: "192.0.2.1"\nhealth_api_host: "{configured}"\n'
    )
    script = (_REPO / "scripts/install_guardian.sh").read_text()
    target = script.split("# Auto-detect health API port", 1)[1].split(
        'VENV_DIR="$INSTALL_DIR/.venv"', 1
    )[0]
    target = "# Auto-detect health API port" + target
    phase = script.split("# ── Step 5:", 1)[1].split("# ── Step 6:", 1)[0]
    # Skip only the phase's descriptive heading, retaining its actual commands.
    phase = phase[phase.index('echo ""') :]
    body = (
        """set -euo pipefail
INSTALL_DIR=$1
VENV_DIR=$2
_listener=$3
CONTAINER_NAME=fixture
incus() { printf '%s\n' "$_listener"; }
"""
        + target
        + phase
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            body,
            "installer-config-test",
            str(install),
            str(Path(sys.executable).parent.parent),
            listener,
        ],
        cwd=_REPO,
        env={"PATH": os.defpath, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    settings = yaml.safe_load(config.read_text())
    assert settings["health_api_host"] == expected
    assert settings["container_ip"] == "192.0.2.1"
    assert "# retain operator note" in config.read_text()


@pytest.mark.parametrize("layout", ["existing", "fresh", "partial", "same_directory"])
def test_code_copy_and_generation_preserve_operator_config(tmp_path, layout):
    source = tmp_path / "repo"
    (source / "src/genesis/guardian").mkdir(parents=True)
    (source / "src/genesis/__init__.py").touch()
    (source / "src/genesis/guardian/__init__.py").touch()
    for name in ("config.py", "dashboard_ingress.py"):
        shutil.copy2(_REPO / "src/genesis/guardian" / name, source / "src/genesis/guardian" / name)
    (source / "scripts").mkdir()
    (source / "scripts/shipped.sh").write_text("# shipped\n")
    (source / "config/nested").mkdir(parents=True)
    (source / "config/nested/default.yaml").write_text("setting: shipped\n")
    (source / "config/guardian.yaml").write_text("container_name: shipped-template\n")
    install = source if layout == "same_directory" else tmp_path / "installed"
    if layout in ("existing", "partial", "same_directory"):
        (install / "config").mkdir(parents=True, exist_ok=True)
        (install / "config/guardian.yaml").write_text(
            "# operator note\ncontainer_name: operator-container\n"
            'container_ip: "192.0.2.1"\nhealth_api_host: "localhost"\n'
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
VENV_DIR=$4
CONTAINER_NAME=detected-container
CONTAINER_IP=192.0.2.2
HEALTH_HOST=127.0.0.1
HEALTH_PORT=5000
CC_ENABLED=false
CLAUDE_PATH=/fixture/claude
"""
        + copy_phase
        + generation
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            body,
            "installer-copy-test",
            str(source),
            str(install),
            sys.executable,
            str(Path(sys.executable).parent.parent),
        ],
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
        assert settings["health_api_host"] == "127.0.0.1"
    else:
        assert settings["container_name"] == "operator-container"
        assert settings["container_ip"] == "192.0.2.1"
        assert settings["health_api_host"] == "localhost"
        assert "# operator note" in content
    assert (install / "config/nested/default.yaml").read_text() == "setting: shipped\n"
    assert (install / "scripts/shipped.sh").read_text() == "# shipped\n"
