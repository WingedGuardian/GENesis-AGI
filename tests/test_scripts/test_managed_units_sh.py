"""scripts/lib/managed_units.sh: install a stamped render, or keep a hand-edited unit.

Drives the real shell functions (bootstrap.sh's render loop and setup-vnc.sh call
them) with the real checker, against a scratch git repository and unit directory.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib" / "managed_units.sh"
CHECKER = REPO / "scripts" / "lib" / "managed_units.py"
BOOTSTRAP = REPO / "scripts" / "bootstrap.sh"
TEMPLATE = "[Service]\nExecStart=__VENV__/bin/python -m demo\nRestart=on-failure\n"
RENDER = TEMPLATE.replace("__VENV__", "/opt/v")


def _git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.invalid",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.invalid",
    )
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, env=env
    ).stdout.strip()


def _stamp(text: str) -> str:
    return subprocess.run(
        ["python3", "-I", "-S", str(CHECKER), "stamp"],
        input=text,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def world(tmp_path: Path):
    repo, home = tmp_path / "repo", tmp_path / "home"
    (repo / "scripts" / "systemd").mkdir(parents=True)
    (repo / "scripts" / "lib").mkdir(parents=True)
    (repo / "scripts" / "lib" / "managed_units.py").write_text(CHECKER.read_text())
    (repo / "scripts" / "systemd" / "demo.service.template").write_text(TEMPLATE)
    _git(repo, "init", "-q", "-b", "trunk")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (home / ".genesis" / "config").mkdir(parents=True)
    return repo, home, units


def _install(repo: Path, home: Path, units: Path, take: str = "") -> subprocess.CompletedProcess:
    summary = home / "summary"
    render = home / "render-dir" / "render"
    render.parent.mkdir(exist_ok=True)
    render.write_text(RENDER)
    script = f"""
set -euo pipefail
GENESIS_ROOT={str(repo)!r}; HOME={str(home)!r}
export GENESIS_DEPLOY_SUMMARY={str(summary)!r} GENESIS_TAKE_TEMPLATES={take!r}
source {str(LIB)!r}
genesis_install_managed_unit scripts/systemd/demo.service.template {str(render)!r} {str(units / "demo.service")!r}
echo "changed=$GENESIS_MU_CHANGED kept=$GENESIS_MU_KEPT"
"""
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True)


def test_a_missing_unit_is_created_stamped(world):
    repo, home, units = world
    r = _install(repo, home, units)
    assert r.returncode == 0, r.stderr
    assert "Created: demo.service" in r.stdout and "changed=1" in r.stdout
    assert (units / "demo.service").read_text() == _stamp(RENDER)


def test_an_unstamped_genesis_render_is_stamped_in_place(world):
    repo, home, units = world
    (units / "demo.service").write_text(RENDER)
    r = _install(repo, home, units)
    assert "Updated: demo.service" in r.stdout
    assert (units / "demo.service").read_text() == _stamp(RENDER)


def test_an_identical_stamped_unit_is_left_alone(world):
    repo, home, units = world
    (units / "demo.service").write_text(_stamp(RENDER))
    r = _install(repo, home, units)
    assert "OK: demo.service (unchanged)" in r.stdout and "changed=0" in r.stdout


@pytest.mark.parametrize(
    "edited", [_stamp(RENDER) + "Environment=X=1\n", "[Service]\nExecStart=/mine\n"]
)
def test_a_hand_edited_unit_is_kept_and_named(world, edited):
    repo, home, units = world
    (units / "demo.service").write_text(edited)
    r = _install(repo, home, units)
    assert r.returncode == 0, r.stderr
    assert (units / "demo.service").read_text() == edited
    assert "Kept: demo.service (edited by hand)" in r.stdout and "kept=1" in r.stdout
    assert "kept demo.service" in (home / "summary").read_text()


def test_a_taken_unit_is_backed_up_then_replaced(world):
    repo, home, units = world
    (units / "demo.service").write_text("[Service]\nExecStart=/mine\n")
    r = _install(repo, home, units, take="demo.service")
    assert "Updated: demo.service" in r.stdout
    backups = list((home / ".genesis" / "deploy-backups").glob("*/demo.service"))
    assert len(backups) == 1 and backups[0].read_text() == "[Service]\nExecStart=/mine\n"
    assert (units / "demo.service").read_text() == _stamp(RENDER)
    assert "taken demo.service" in (home / "summary").read_text()


def test_a_masked_unit_is_never_written_through(world):
    repo, home, units = world
    (units / "demo.service").symlink_to("/dev/null")
    r = _install(repo, home, units)
    assert r.returncode == 0, r.stderr
    assert os.readlink(units / "demo.service") == "/dev/null"
    assert "Kept: demo.service (a symlink" in r.stdout


def test_a_taken_symlink_is_saved_and_replaced_by_a_regular_file(world, tmp_path):
    repo, home, units = world
    elsewhere = tmp_path / "elsewhere.service"
    elsewhere.write_text("mine\n")
    (units / "demo.service").symlink_to(elsewhere)
    r = _install(repo, home, units, take="*")
    assert r.returncode == 0, r.stderr
    assert not (units / "demo.service").is_symlink()
    assert elsewhere.read_text() == "mine\n", "wrote through the link"
    assert list((home / ".genesis" / "deploy-backups").glob("*/demo.service"))[0].is_symlink()


def test_a_directory_target_is_kept_even_when_taken(world):
    repo, home, units = world
    (units / "demo.service").mkdir()
    r = _install(repo, home, units, take="*")
    assert r.returncode == 0 and (units / "demo.service").is_dir()


@pytest.mark.parametrize(
    ("listing", "opted_out"),
    [
        ("genesis-watchdog.timer\n", True),
        ("  genesis-watchdog.timer   # off while debugging\n", True),
        ("# genesis-watchdog.timer\n", False),
        ("genesis-watchdog.timer.bak\n", False),
        ("genesis-watchdog.timer", True),  # no trailing newline
    ],
)
def test_the_timer_opt_out_file(world, listing, opted_out):
    repo, home, _ = world
    (home / ".genesis" / "config" / "disabled_timers").write_text(listing)
    script = (
        f"set -euo pipefail; GENESIS_ROOT={str(repo)!r}; HOME={str(home)!r}; unset GENESIS_HOME\n"
        f"source {str(LIB)!r}\n"
        "if genesis_timer_opted_out genesis-watchdog.timer; then echo OFF; else echo ON; fi\n"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.stdout.strip() == ("OFF" if opted_out else "ON"), r.stderr


def _bootstrap_refusal_block() -> str:
    text = BOOTSTRAP.read_text()
    start = text.index("# --- Managed units: a run summary, and (run by hand)")
    return text[start : text.index("# --- Prerequisites ---")]


@pytest.mark.parametrize(
    ("allow_live", "take", "code"), [("", "", 4), ("1", "", 0), ("", "demo.service", 0)]
)
def test_bootstrap_by_hand_refuses_a_hand_edit_before_anything(world, allow_live, take, code):
    repo, home, units = world
    (units / "demo.service").write_text("[Service]\nExecStart=/mine\n")
    script = (
        f"set -euo pipefail\nGENESIS_ROOT={str(repo)!r}; HOME={str(home)!r}\n"
        f"SCRIPT_DIR={str(repo / 'scripts')!r}\n"
        f"export GENESIS_BOOTSTRAP_ALLOW_LIVE={allow_live!r} GENESIS_TAKE_TEMPLATES={take!r}\n"
        "unset GENESIS_DEPLOY_SUMMARY\n" + _bootstrap_refusal_block() + "echo CONTINUED\n"
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == code, r.stdout + r.stderr
    assert ("CONTINUED" in r.stdout) is (code == 0)
    if code == 4:
        assert "REFUSED: demo.service" in r.stdout
