"""scripts/install_browser_stack.sh: one run at a time, a resolvable HOME, and the
provisioner's own exit code. The provisioner is a stub interpreter in a fake repo."""

from __future__ import annotations

import fcntl
import subprocess
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "install_browser_stack.sh"


@pytest.fixture
def repo(tmp_path):
    """A repo dir whose .venv/bin/python records its arguments and exits with $STUB_RC."""
    r = tmp_path / "repo"
    (r / ".venv" / "bin").mkdir(parents=True)
    py = r / ".venv" / "bin" / "python"
    py.write_text('#!/bin/sh\necho "$@" > "$STUB_LOG"\nexit "${STUB_RC:-0}"\n')
    py.chmod(0o755)
    return r


def _run(repo, tmp_path, *, rc=0, env_extra=None, drop_home=False):
    env = {
        # Hermetic: a Genesis venv on the caller's PATH would be preferred.
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "GENESIS_HOME": str(tmp_path / "gh"),
        "GENESIS_REPO_DIR": str(repo),
        "STUB_LOG": str(tmp_path / "stub.log"),
        "STUB_RC": str(rc),
        **(env_extra or {}),
    }
    if drop_home:
        env.pop("HOME")
    return subprocess.run(
        ["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False
    )


@pytest.mark.parametrize("rc", [0, 1])
def test_runs_the_provisioner_and_passes_its_exit_code(repo, tmp_path, rc):
    r = _run(repo, tmp_path, rc=rc)
    assert r.returncode == rc, r.stdout + r.stderr
    assert (tmp_path / "stub.log").read_text().split() == ["-m", "genesis.browser.provision", "run"]


def test_a_second_run_exits_0_while_the_first_holds_the_lock(repo, tmp_path):
    lock_dir = tmp_path / "gh" / "locks"
    lock_dir.mkdir(parents=True)
    with open(lock_dir / "browser-stack.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = _run(repo, tmp_path, rc=1)
    assert r.returncode == 0
    assert "another run is in progress" in r.stdout
    assert not (tmp_path / "stub.log").exists()


def test_no_interpreter_is_exit_75(repo, tmp_path):
    (repo / ".venv" / "bin" / "python").unlink()
    r = _run(repo, tmp_path)
    assert r.returncode == 75 and "no venv interpreter" in r.stdout


def test_an_unset_home_is_resolved_from_passwd(repo, tmp_path):
    r = _run(repo, tmp_path, drop_home=True, env_extra={"GENESIS_HOME": str(tmp_path / "gh")})
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_unit_template_runs_this_script_without_escalation():
    unit = (_SCRIPT.parent / "systemd" / "genesis-browser-stack.service.template").read_text()
    # Quoted: systemd splits an unquoted rendered path at its first space.
    assert 'ExecStart=/bin/bash "__REPO_DIR__/scripts/install_browser_stack.sh"' in unit
    assert "NoNewPrivileges=true" in unit and "Type=oneshot" in unit
    lines = [ln.strip() for ln in unit.splitlines() if not ln.strip().startswith("#")]
    assert "[Install]" not in lines  # started by its timer or an operator, never at login
    # The module sets TMPDIR for its download children only (CLAUDE.md: never in a unit).
    assert not any(ln.startswith("Environment") and "TMPDIR" in ln for ln in lines)


def test_venv_path_names_the_interpreter(repo, tmp_path):
    """install.sh accepts a VENV_PATH outside the checkout, as genesis.env does."""
    venv = tmp_path / "elsewhere"
    (venv / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").rename(venv / "bin" / "python")
    r = _run(repo, tmp_path, env_extra={"VENV_PATH": str(venv)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "stub.log").read_text().split() == ["-m", "genesis.browser.provision", "run"]


def test_the_timer_retries_daily_and_catches_up():
    timer = (_SCRIPT.parent / "systemd" / "genesis-browser-stack.timer.template").read_text()
    assert "OnCalendar=*-*-* " in timer and "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer  # the installers' timer loop enables it


def test_the_unit_allows_the_runs_whole_budget():
    """Each step's timeout is cut to what is left of provision.RUN_BUDGET_S, which
    stays inside the unit's own limit."""
    from genesis.browser import provision

    unit = (_SCRIPT.parent / "systemd" / "genesis-browser-stack.service.template").read_text()
    limit = int(
        next(ln.split("=", 1)[1] for ln in unit.splitlines() if ln.startswith("TimeoutStartSec="))
    )
    assert limit == 28800
    assert limit > provision.RUN_BUDGET_S


def test_no_sudoers_rule_is_recommended():
    """Any rule naming the venv's python grants root to whoever can write the venv
    (its package code, or a shadow module in the working directory)."""
    assert "NOPASSWD" not in _SCRIPT.read_text()


def test_the_unit_quotes_its_path_and_loads_no_environment_file():
    """An unquoted assignment splits at whitespace in the checkout path. An
    EnvironmentFile= would hand every secret to the downloaders and override the
    unit's own PATH (measured on systemd 255, genesis-disk-hygiene's template)."""
    unit = (_SCRIPT.parent / "systemd" / "genesis-browser-stack.service.template").read_text()
    assert 'Environment="PATH=__VENV__/bin:' in unit
    assert "EnvironmentFile" not in unit


def _env_stub(repo, tmp_path):
    py = repo / ".venv" / "bin" / "python"
    out = tmp_path / "env.txt"
    py.write_text(
        "#!/bin/sh\n"
        f'printf "%s|%s|%s\\n" "$PLAYWRIGHT_BROWSERS_PATH" "$OPENAI_API_KEY" "$GENESIS_BIG_TMP" > "{out}"\n'
    )
    py.chmod(0o755)
    return out


def test_the_runtimes_browser_settings_come_from_secrets_env_by_name(repo, tmp_path):
    """The oneshot must provision where the runtime launches from, and the runtime
    reads these from secrets.env; nothing else from that file reaches it."""
    out = _env_stub(repo, tmp_path)
    (repo / "secrets.env").write_text(
        'PLAYWRIGHT_BROWSERS_PATH="/data/pw"\nOPENAI_API_KEY=sk-not-for-you\n'
        "GENESIS_BIG_TMP=/data/tmp\nPATH=/nowhere\n"
    )
    r = _run(repo, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert out.read_text().strip() == "/data/pw||/data/tmp"


def test_a_secrets_path_override_is_honoured(repo, tmp_path):
    out = _env_stub(repo, tmp_path)
    elsewhere = tmp_path / "elsewhere.env"
    elsewhere.write_text("PLAYWRIGHT_BROWSERS_PATH=/other/pw\n")
    r = _run(repo, tmp_path, env_extra={"SECRETS_PATH": str(elsewhere)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert out.read_text().split("|")[0] == "/other/pw"


def test_with_a_wait_it_runs_once_the_other_run_finishes(repo, tmp_path):
    """install.sh runs inline right after enabling the daily timer, whose
    catch-up run can take the lock first. Waiting, then running (a no-op after a
    complete run), reports the real outcome instead of a bare exit 0."""
    lock_dir = tmp_path / "gh" / "locks"
    lock_dir.mkdir(parents=True)
    holder = subprocess.Popen(["flock", str(lock_dir / "browser-stack.lock"), "sleep", "2"])
    try:
        time.sleep(0.5)
        r = _run(repo, tmp_path, rc=1, env_extra={"GENESIS_BROWSER_STACK_WAIT_S": "30"})
    finally:
        holder.wait(timeout=30)
    assert r.returncode == 1, r.stdout + r.stderr
    assert (tmp_path / "stub.log").read_text().split() == ["-m", "genesis.browser.provision", "run"]


def test_a_wait_that_runs_out_is_exit_75(repo, tmp_path):
    lock_dir = tmp_path / "gh" / "locks"
    lock_dir.mkdir(parents=True)
    with open(lock_dir / "browser-stack.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        r = _run(repo, tmp_path, env_extra={"GENESIS_BROWSER_STACK_WAIT_S": "1"})
    assert r.returncode == 75
    assert "still in progress" in r.stdout
    assert not (tmp_path / "stub.log").exists()


def _path_python(tmp_path, *, genesis_venv):
    """A `python` first on PATH, as a unit rendered before it carried VENV_PATH
    has (its PATH starts with the venv's bin)."""
    bindir = tmp_path / "pathvenv" / "bin"
    bindir.mkdir(parents=True)
    py = bindir / "python"
    check_rc = 0 if genesis_venv else 1
    py.write_text(
        "#!/bin/sh\n"
        f'if [ "$3" = "venv-check" ]; then exit {check_rc}; fi\n'
        'echo "$@" > "$STUB_LOG"\n'
        f'touch "{bindir.parent}/ran"\n'
    )
    py.chmod(0o755)
    return f"{bindir}:/usr/bin:/bin"


def test_without_venv_path_the_venv_on_path_is_used(repo, tmp_path):
    (repo / ".venv" / "bin" / "python").unlink()
    r = _run(repo, tmp_path, env_extra={"PATH": _path_python(tmp_path, genesis_venv=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    assert (tmp_path / "stub.log").read_text().split() == ["-m", "genesis.browser.provision", "run"]


def test_a_python_on_path_that_is_not_a_genesis_venv_is_refused(repo, tmp_path):
    (repo / ".venv" / "bin" / "python").unlink()
    r = _run(repo, tmp_path, env_extra={"PATH": _path_python(tmp_path, genesis_venv=False)})
    assert r.returncode == 75
    assert not (tmp_path / "stub.log").exists()


def test_the_venv_on_path_wins_over_a_checkout_venv(repo, tmp_path):
    """An older unit names its configured venv only through PATH; a checkout
    .venv left beside it may be stale."""
    r = _run(repo, tmp_path, env_extra={"PATH": _path_python(tmp_path, genesis_venv=True)})
    assert r.returncode == 0, r.stdout + r.stderr
    # The PATH stub writes the run's arguments; the checkout stub would too, so
    # tell them apart by which one ran the import check.
    assert (tmp_path / "stub.log").read_text().split() == ["-m", "genesis.browser.provision", "run"]
    assert (tmp_path / "pathvenv" / "ran").exists()
