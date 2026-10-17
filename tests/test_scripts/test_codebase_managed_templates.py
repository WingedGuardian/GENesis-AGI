"""Exercise both ordinary renderer loops, including literal Exec argument paths."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UNITS = ("genesis-cbm-query.service", "genesis-cbm-query-clients.slice")


def loop(renderer, with_post=False):
    source = (ROOT / "scripts" / renderer).read_text()
    start = source.index('    for template in "$SYSTEMD_TEMPLATE_DIR"/')
    end = source.index("    done\n", start) + len("    done\n")
    if with_post:
        if renderer == "bootstrap.sh":
            end = source.index("    # Enable the cc-tmp cold-start apply SERVICE", end)
        else:
            end = source.index("# Step 8 removed", end)
            # This segment closes install's enclosing template-directory check.
            return "if true; then\n" + source[start:end]
    return source[start:end]


def prelude(scratch):
    """What both scripts set up before their render loops: SCRIPT_DIR (install's
    unit stamp) and bootstrap's managed-unit writer with its render file. The
    writer is sourced against this repository, then GENESIS_ROOT is restored to
    the test's literal home so the Exec path substitutions stay under test."""
    scripts = shlex.quote(str(ROOT / "scripts"))
    return (
        f"SCRIPT_DIR={scripts}\n"
        '_test_root="$GENESIS_ROOT"\n'
        f"GENESIS_ROOT={shlex.quote(str(ROOT))}\n"
        '. "$SCRIPT_DIR/lib/managed_units.sh"\n'
        'GENESIS_ROOT="$_test_root"\n'
        f"_mu_dir=$(mktemp -d -p {shlex.quote(str(scratch))})\n"
        '_mu_render="$_mu_dir/render"\n'
    )


@pytest.fixture
def render(tmp_path):
    templates, units = tmp_path / "templates", tmp_path / "units"
    templates.mkdir()
    units.mkdir()
    for name in UNITS:
        shutil.copy2(ROOT / "scripts/systemd" / (name + ".template"), templates)

    def run(renderer, home=None, *, take="", tier="0", with_post=False):
        env = dict(
            os.environ,
            SYSTEMD_TEMPLATE_DIR=str(templates),
            SYSTEMD_USER_DIR=str(units),
            HOME=str(home or tmp_path),
            REPO_DIR=str(home or tmp_path),
            GENESIS_ROOT=str(home or tmp_path),
            VENV_PATH=str(tmp_path / 'venv=$%&|λ" '),
            CC_BIN_DIR="/usr/bin",
            SERVICES_GENERATED="0",
            SERVICES_UPDATED="0",
            FALKORDB_VERSION="4.20.4",
            GENESIS_TAKE_TEMPLATES=take,
            GENESIS_UPDATE_TIER=tier,
            GENESIS_DEPLOY_SUMMARY=str(tmp_path / "summary"),
            TEST_MANAGER_LOG=str(tmp_path / "manager.log"),
            TEST_SCRATCH_PATH=str(tmp_path / "scratch-path"),
        )
        return subprocess.run(
            [
                "/bin/bash",
                "-euc",
                "_falkordb_redis_server_bin() { printf /usr/bin/redis-server; }\n"
                'systemctl() { printf "%s\\n" "$*" >> "$TEST_MANAGER_LOG"; }\n'
                'loginctl() { printf "Linger=yes\\n"; }\n'
                + prelude(tmp_path)
                + 'printf "%s" "$_mu_dir" > "$TEST_SCRATCH_PATH"\n'
                + loop(renderer, with_post),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    return units, run


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
@pytest.mark.parametrize("name", UNITS)
def test_mask_kept_and_renderer_reaches_reload_and_timers(render, renderer, name):
    units, run = render
    target = units / name
    target.symlink_to("/dev/null")
    before = target.lstat()
    templates = units.parent / "templates"
    (templates / "continuity.timer.template").write_text("[Timer]\nOnBootSec=1h\n")
    result = run(renderer, with_post=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert target.is_symlink() and os.readlink(target) == "/dev/null"
    assert (target.lstat().st_dev, target.lstat().st_ino) == (before.st_dev, before.st_ino)
    assert name in result.stdout and "kept" in result.stdout.lower()
    commands = (units.parent / "manager.log").read_text().splitlines()
    assert "--user daemon-reload" in commands
    if renderer == "bootstrap.sh":
        assert commands.index("--user daemon-reload") < commands.index("--user enable --now continuity.timer")
        scratch = Path((units.parent / "scratch-path").read_text())
        assert not scratch.exists(), "renderer scratch survived successful bootstrap"
        assert f"kept {name}:" in (units.parent / "summary").read_text()


@pytest.mark.parametrize("name", UNITS)
@pytest.mark.parametrize("take", ["", "*", "named"])
@pytest.mark.parametrize("tier", ["0", "1"])
def test_bootstrap_mask_take_is_explicit_and_backed_up(render, name, take, tier):
    units, run = render
    target = units / name
    for unit in UNITS:
        (units / unit).symlink_to("/dev/null")
    other = units / next(unit for unit in UNITS if unit != name)
    other_inode = other.lstat().st_ino
    before = target.lstat().st_ino
    result = run("bootstrap.sh", take=name if take == "named" else take, tier=tier)
    assert result.returncode == 0, result.stdout + result.stderr
    assert other.is_symlink() and os.readlink(other) == "/dev/null"
    assert other.lstat().st_ino == other_inode
    if take == "named" and tier == "0":
        assert not target.is_symlink() and target.is_file()
        assert "genesis-managed" in target.read_text()
        backups = list((units.parent / ".genesis/deploy-backups").glob("*/" + name))
        assert len(backups) == 1 and backups[0].is_symlink()
        assert os.readlink(backups[0]) == "/dev/null"
    else:
        assert target.is_symlink() and os.readlink(target) == "/dev/null"
        assert target.lstat().st_ino == before


@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
@pytest.mark.parametrize(
    "suffix", ["plain", "trailing ", 'quote"back\\slash', "=$value%&|unicode-λ"]
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
    venv = home / ".venv" if renderer == "bootstrap.sh" else tmp_path / 'venv=$%&|λ" '
    venv_escaped = str(venv).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    for entry in ("ExecStart", "ExecStartPost"):
        assert f"{entry}=:/bin/sh -c 'exec \"$@\"' -- \"{venv_escaped}/bin/python\" -I \"{escaped}/scripts/codebase_managed.py\"" in body
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
    if kind in ("link", "dangling"):
        assert result.returncode == 0, result.stdout + result.stderr
        assert name in result.stdout and "kept" in result.stdout.lower()
        assert target.is_symlink() and os.readlink(target) == str(foreign)
    else:
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
    result = run(renderer)
    assert result.returncode == 0, result.stderr
    # Neither renderer replaces a unit it did not write. install.sh leaves an
    # existing unit alone; bootstrap's managed-unit writer keeps an unstamped
    # unit and names it. Whether it was classified as a hand edit is not
    # asserted here: that is lib/managed_units.sh's own tests' job.
    assert ordinary.read_text() == "foreign ordinary"
    if renderer == "bootstrap.sh":
        assert "Kept: ordinary.service" in result.stdout, result.stdout
