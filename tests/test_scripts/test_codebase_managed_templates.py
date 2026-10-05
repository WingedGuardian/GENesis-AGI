"""Exercise both ordinary renderer loops, including literal Exec argument paths."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UNITS = ("genesis-cbm-query.service", "genesis-cbm-query-clients.slice")


def loop(renderer):
    source = (ROOT / "scripts" / renderer).read_text()
    start = source.index('    for template in "$SYSTEMD_TEMPLATE_DIR"/')
    end = source.index("    done\n", start) + len("    done\n")
    return source[start:end]


@pytest.fixture
def render(tmp_path):
    templates, units = tmp_path / "templates", tmp_path / "units"
    templates.mkdir()
    units.mkdir()
    for name in UNITS:
        shutil.copy2(ROOT / "scripts/systemd" / (name + ".template"), templates)

    def run(renderer, home=None):
        env = dict(
            os.environ,
            SYSTEMD_TEMPLATE_DIR=str(templates),
            SYSTEMD_USER_DIR=str(units),
            HOME=str(home or tmp_path),
            REPO_DIR=str(home or tmp_path),
            GENESIS_ROOT=str(home or tmp_path),
            VENV_PATH=str(tmp_path / ".venv"),
            CC_BIN_DIR="/usr/bin",
            SERVICES_GENERATED="0",
            SERVICES_UPDATED="0",
            FALKORDB_VERSION="4.20.4",
        )
        return subprocess.run(
            [
                "/bin/bash",
                "-euc",
                "_falkordb_redis_server_bin() { printf /usr/bin/redis-server; }\n" + loop(renderer),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    return units, run


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
@pytest.mark.parametrize(
    "suffix", ["plain", "trailing ", 'quote"back\\slash', "$value%&|unicode-λ"]
)
def test_rendered_exec_paths_are_literal_and_templates_are_unconditional(
    render, tmp_path, renderer, suffix
):
    units, run = render
    home = tmp_path / suffix
    result = run(renderer, home)
    assert result.returncode == 0, result.stderr
    assert {p.name for p in units.iterdir()} == set(UNITS)
    body = (units / UNITS[0]).read_text()
    escaped = str(home).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    assert f'ExecStart=:/usr/bin/python3 -I "{escaped}/scripts/codebase_managed.py"' in body
    assert f'--config "{escaped}/.genesis/config/codebase-managed.json" serve' in body
    assert "__" not in body
    assert "MemoryMax=2G" in body and "MemorySwapMax=0" in body
    assert "Restart=no" in body and "KillMode=control-group" in body


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
@pytest.mark.parametrize("name", UNITS)
@pytest.mark.parametrize("kind", ["link", "dangling", "directory", "fifo"])
def test_nonregular_template_destination_is_preserved(render, tmp_path, renderer, name, kind):
    units, run = render
    target, foreign = units / name, tmp_path / "foreign"
    if kind in ("link", "dangling"):
        if kind == "link":
            foreign.write_text("foreign")
        target.symlink_to(foreign)
    elif kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)
    before = target.lstat().st_ino
    result = run(renderer)
    assert result.returncode != 0 and "nonregular managed Codebase unit" in result.stderr
    assert target.lstat().st_ino == before
    if kind == "link":
        assert foreign.read_text() == "foreign"


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
def test_ordinary_units_keep_existing_renderer_behavior(render, renderer):
    units, run = render
    ordinary = units / "ordinary.service"
    ordinary.write_text("foreign ordinary")
    template = units.parent / "templates/ordinary.service.template"
    template.write_text("[Service]\nExecStart=/bin/true\n")
    assert run(renderer).returncode == 0
    expected = "foreign ordinary" if renderer == "install.sh" else template.read_text()
    assert ordinary.read_text() == expected
