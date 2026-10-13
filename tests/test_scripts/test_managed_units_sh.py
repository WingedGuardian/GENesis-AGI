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
    assert "Taken: demo.service" in r.stdout
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


def test_a_blanket_take_never_unmasks(world):
    repo, home, units = world
    (units / "demo.service").symlink_to("/dev/null")
    r = _install(repo, home, units, take="*")
    assert r.returncode == 0, r.stderr
    assert os.readlink(units / "demo.service") == "/dev/null"
    assert "only --take-template demo.service replaces it" in r.stdout
    assert not (home / ".genesis" / "deploy-backups").exists()


def test_a_symlink_taken_by_name_is_saved_and_replaced_by_a_regular_file(world, tmp_path):
    repo, home, units = world
    elsewhere = tmp_path / "elsewhere.service"
    elsewhere.write_text("mine\n")
    (units / "demo.service").symlink_to(elsewhere)
    r = _install(repo, home, units, take="demo.service")
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


def test_a_bootstrap_refusal_keeps_a_summary_file_it_did_not_create(world):
    """GENESIS_DEPLOY_SUMMARY may name a caller's file; a refusal must not delete it."""
    repo, home, units = world
    (units / "demo.service").write_text("[Service]\nExecStart=/mine\n")
    theirs = home / "callers-log.txt"
    theirs.write_text("keep me\n")
    script = (
        f"set -euo pipefail\nGENESIS_ROOT={str(repo)!r}; HOME={str(home)!r}\n"
        f"SCRIPT_DIR={str(repo / 'scripts')!r}\n"
        f"export GENESIS_BOOTSTRAP_ALLOW_LIVE='' GENESIS_TAKE_TEMPLATES=''\n"
        f"export GENESIS_DEPLOY_SUMMARY={str(theirs)!r}\n" + _bootstrap_refusal_block()
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 4, r.stdout + r.stderr
    assert theirs.read_text() == "keep me\n"


def _install_step7_block() -> str:
    text = (REPO / "scripts" / "install.sh").read_text()
    start = text.index('    for template in "$SYSTEMD_TEMPLATE_DIR"/*.service.template')
    return text[start : text.index("\n    done\n", start) + len("\n    done\n")]


def test_install_writes_stamped_units_and_never_overwrites(tmp_path):
    """A unit install.sh creates carries its stamp from the start, so the first
    update can tell an edit to a token's value from the template (#3138 review)."""
    tdir, udir = tmp_path / "templates", tmp_path / "units"
    tdir.mkdir()
    udir.mkdir()
    (tdir / "demo.service.template").write_text(TEMPLATE)
    (tdir / "kept.service.template").write_text(TEMPLATE)
    (udir / "kept.service").write_text("[Service]\nExecStart=/theirs\n")
    script = (
        "set -euo pipefail\n"
        f"SYSTEMD_TEMPLATE_DIR={str(tdir)!r} SYSTEMD_USER_DIR={str(udir)!r}\n"
        f"HOME={str(tmp_path)!r} VENV_PATH=/opt/v REPO_DIR=/opt/r CC_BIN_DIR=/opt/c\n"
        f"SCRIPT_DIR={str(REPO / 'scripts')!r}\n"
        "_falkordb_redis_server_bin() { echo /usr/bin/redis-server; }\n" + _install_step7_block()
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    rendered = (udir / "demo.service").read_text()
    stamp = subprocess.run(
        ["python3", "-I", "-S", str(CHECKER), "stamp"], input=RENDER, capture_output=True, text=True
    ).stdout
    assert rendered == stamp
    assert (udir / "kept.service").read_text() == "[Service]\nExecStart=/theirs\n"
    assert sorted(p.name for p in udir.iterdir()) == ["demo.service", "kept.service"]


@pytest.mark.parametrize(
    "link_to", ["/dev/null", "/nonexistent/dangling"], ids=["masked", "dangling"]
)
def test_an_install_rerun_never_replaces_a_masked_unit(tmp_path, link_to):
    """A masked unit is a symlink to /dev/null; replacing it would unmask it, and
    the timer loop that follows would then enable it (#3138 review)."""
    tdir, udir = tmp_path / "templates", tmp_path / "units"
    tdir.mkdir()
    udir.mkdir()
    (tdir / "masked.timer.template").write_text(TEMPLATE)
    (udir / "masked.timer").symlink_to(link_to)
    script = (
        "set -euo pipefail\n"
        f"SYSTEMD_TEMPLATE_DIR={str(tdir)!r} SYSTEMD_USER_DIR={str(udir)!r}\n"
        f"HOME={str(tmp_path)!r} VENV_PATH=/opt/v REPO_DIR=/opt/r CC_BIN_DIR=/opt/c\n"
        f"SCRIPT_DIR={str(REPO / 'scripts')!r}\n"
        "_falkordb_redis_server_bin() { echo /usr/bin/redis-server; }\n" + _install_step7_block()
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (udir / "masked.timer").is_symlink()
    assert os.readlink(udir / "masked.timer") == link_to
    assert "masked.timer is a symlink or not a regular file (masked?) — kept" in r.stdout


def _no_change_branch() -> str:
    # Anchored on the neighbours, not on the condition under test, so a mutated
    # condition still extracts (and fails the assertion, not the extraction).
    text = (REPO / "scripts" / "update.sh").read_text()
    start = text.index("# END tier2-baseline-check\n") + len("# END tier2-baseline-check\n")
    end = text.index('    echo "  Already up to date ($NEW_COMMIT)."', start)
    end = text.rindex("elif ", start, end)
    return (
        text[start:end] + 'elif [[ "$OLD_COMMIT" == "$NEW_COMMIT" ]]; then\n    echo NOCHANGE\nfi\n'
    )


@pytest.mark.parametrize(("take", "activates"), [("demo.service", True), ("", False)])
def test_a_take_with_nothing_new_still_runs_bootstrap(take, activates):
    """Only bootstrap installs a taken template, so --take-template must not stop
    at "Already up to date" (#3138 review)."""
    script = (
        "OLD_COMMIT=abc NEW_COMMIT=abc\n"
        f"GENESIS_TAKE_TEMPLATES={take!r}\n"
        "_tier2_pending_since_baseline() { return 1; }\n" + _no_change_branch()
    )
    out = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout
    assert ("NOCHANGE" not in out) is activates, out
    assert ("--take-template was given" in out) is activates


def test_an_edit_after_the_precheck_is_kept_not_overwritten(world):
    """The precheck's verdict was reached on the old bytes; a unit edited after it
    (before its turn in the render loop) is judged again, never overwritten
    (#3138 review)."""
    repo, home, units = world
    unit = units / "demo.service"
    unit.write_text(_stamp(RENDER.replace("on-failure", "always")))
    render = home / "render-dir" / "render"
    render.parent.mkdir(exist_ok=True)
    render.write_text(RENDER)
    script = f"""
set -euo pipefail
GENESIS_ROOT={str(repo)!r}; HOME={str(home)!r}
source {str(LIB)!r}
genesis_mu_precheck {str(units)!r} {str(render.parent)!r}
printf 'Environment=X=1\\n' >> {str(unit)!r}
genesis_install_managed_unit scripts/systemd/demo.service.template {str(render)!r} {str(unit)!r}
echo "kept=$GENESIS_MU_KEPT"
"""
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert unit.read_text().endswith("Environment=X=1\n")
    assert "Kept: demo.service (edited by hand)" in r.stdout and "kept=1" in r.stdout


def _replace(new: Path, target: Path, expected: str) -> subprocess.CompletedProcess:
    script = f"""
source {str(LIB)!r}
genesis_mu_replace {str(new)!r} {str(target)!r} {expected!r}
echo "rc=$?"
"""
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "GENESIS_ROOT": "/nonexistent"},
    )


def test_replace_refuses_a_target_that_changed_since_it_was_judged(tmp_path):
    new, snap, target = tmp_path / "new", tmp_path / "snap", tmp_path / "unit"
    new.write_text("new\n")
    snap.write_text("judged\n")
    target.write_text("edited since\n")
    assert "rc=2" in _replace(new, target, str(snap)).stdout
    assert target.read_text() == "edited since\n"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".unit.")] == []


def test_replace_never_writes_through_a_link_that_appeared(tmp_path):
    new, snap, target, elsewhere = (tmp_path / n for n in ("new", "snap", "unit", "elsewhere"))
    new.write_text("new\n")
    snap.write_text("judged\n")
    elsewhere.write_text("judged\n")
    target.symlink_to(elsewhere)
    assert "rc=2" in _replace(new, target, str(snap)).stdout
    assert target.is_symlink() and elsewhere.read_text() == "judged\n"


def test_replace_does_not_create_over_a_file_that_appeared(tmp_path):
    new, target = tmp_path / "new", tmp_path / "unit"
    new.write_text("new\n")
    target.write_text("someone else's\n")
    assert "rc=2" in _replace(new, target, "").stdout
    assert target.read_text() == "someone else's\n"


def test_replace_keeps_the_old_files_mode(tmp_path):
    new, snap, target = tmp_path / "new", tmp_path / "snap", tmp_path / "unit"
    new.write_text("new\n")
    snap.write_text("judged\n")
    target.write_text("judged\n")
    target.chmod(0o640)
    assert "rc=0" in _replace(new, target, str(snap)).stdout
    assert target.read_text() == "new\n" and (target.stat().st_mode & 0o777) == 0o640


def test_a_directory_is_kept_with_a_message_that_does_not_promise_a_take(world):
    repo, home, units = world
    (units / "demo.service").mkdir()
    r = _install(repo, home, units, take="demo.service")
    assert r.returncode == 0, r.stderr
    assert (units / "demo.service").is_dir()
    assert "not a regular file; remove it by hand" in r.stdout
    assert "only --take-template" not in r.stdout


def test_a_supervised_session_never_takes_through_bootstrap(world, monkeypatch):
    """update.sh refuses the take flags in a tier session; the lib refuses an
    inherited take list there too, so bootstrap is not a second door."""
    repo, home, units = world
    (units / "demo.service").write_text("[Service]\nExecStart=/mine\n")
    monkeypatch.setenv("GENESIS_UPDATE_TIER", "1")
    r = _install(repo, home, units, take="*")
    assert (units / "demo.service").read_text() == "[Service]\nExecStart=/mine\n"
    assert "Kept: demo.service (edited by hand)" in r.stdout


def test_a_take_that_loses_the_race_is_reported_only_as_kept(world):
    """The backup is made, then the unit changes before the rename: the summary
    must not also claim the template was installed."""
    repo, home, units = world
    unit = units / "demo.service"
    unit.write_text("[Service]\nExecStart=/mine\n")
    render = home / "render-dir" / "render"
    render.parent.mkdir(exist_ok=True)
    render.write_text(RENDER)
    summary = home / "summary"
    script = f"""
set -euo pipefail
GENESIS_ROOT={str(repo)!r}; HOME={str(home)!r}
export GENESIS_DEPLOY_SUMMARY={str(summary)!r} GENESIS_TAKE_TEMPLATES=demo.service
source {str(LIB)!r}
eval "_real_backup() $(declare -f genesis_mu_backup | tail -n +2)"
genesis_mu_backup() {{ _real_backup "$@" && printf 'X=2\\n' >> {str(unit)!r}; }}
genesis_install_managed_unit scripts/systemd/demo.service.template {str(render)!r} {str(unit)!r}
"""
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert unit.read_text().endswith("X=2\n")
    text = summary.read_text()
    assert "kept demo.service: it changed while" in text and "taken demo.service" not in text


def test_a_render_that_cannot_be_stamped_fails_loudly(world):
    """A unit that cannot be rendered is a failed deploy: the install step returns
    non-zero (bootstrap runs under set -e) and names the unit in the summary,
    which update.sh prints, since the warning itself is cut by `tail -10`."""
    repo, home, units = world
    summary = home / "summary"
    render = home / "render-dir" / "render"
    render.parent.mkdir(exist_ok=True)
    render.write_text(RENDER + "# genesis-managed v1 sha256=" + "0" * 64 + "\n")
    script = f"""
GENESIS_ROOT={str(repo)!r}; HOME={str(home)!r}
export GENESIS_DEPLOY_SUMMARY={str(summary)!r}
source {str(LIB)!r}
genesis_install_managed_unit scripts/systemd/demo.service.template {str(render)!r} {str(units / "demo.service")!r}
echo "rc=$?"
"""
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert "rc=1" in r.stdout, r.stdout + r.stderr
    assert not (units / "demo.service").exists()
    assert "not installed demo.service" in summary.read_text()
