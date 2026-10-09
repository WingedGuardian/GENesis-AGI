"""`editable_install_guarded` (scripts/lib/venv_setup.sh) must fail CLOSED.

An editable install is system-wide state: pointing it at a linked git worktree
redirects EVERY Genesis process — server, bridge, watchdog — to that worktree's
code. That caused an I/O death spiral and repeated crashes on 2026-03-16, and
this guard exists solely to refuse it.

The guard had no tests, which is how it shipped a fail-open: when `git rev-parse`
could not answer, empty variables fell through to the SAME path as a confirmed
non-worktree, and the function performed the install it exists to prevent.
MEASURED before the fix — a non-repo path returned rc=2 ("pip ran, not
importable"), i.e. it had already run pip. The realistic trigger is not an exotic
one: git's `safe.directory` refusal fires whenever the installer runs as a
different user than the repo owner.

"I could not determine whether this is a worktree" is not evidence that it is
not one.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIB = _REPO_ROOT / "scripts" / "lib" / "venv_setup.sh"

RC_OK = 0
RC_BLOCKED = 1
RC_NOT_IMPORTABLE = 2


def _call(repo_dir: str, venv: str = "/nonexistent-venv") -> subprocess.CompletedProcess:
    """Invoke the guard. The venv is deliberately absent: any run that reaches
    pip fails there, so a code other than BLOCKED proves the guard was passed."""
    script = f'source "{_LIB}"; editable_install_guarded "{repo_dir}" "{venv}"'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_a_linked_worktree_is_blocked(tmp_path: Path) -> None:
    """The case the guard was written for."""
    main = tmp_path / "main"
    main.mkdir()
    _git("init", "-q", cwd=main)
    _git("config", "user.email", "t@example.invalid", cwd=main)
    _git("config", "user.name", "t", cwd=main)
    (main / "f").write_text("x\n")
    _git("add", "-A", cwd=main)
    _git("commit", "-qm", "c", cwd=main)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), "-b", "b", cwd=main)

    res = _call(str(wt))

    assert res.returncode == RC_BLOCKED, res.stdout + res.stderr
    assert "worktree" in res.stdout.lower()


@pytest.mark.parametrize(
    "why,path",
    [
        ("path is not a repository at all", "/nonexistent-path-not-a-repo"),
        ("path does not exist", "/proc/self/nonexistent"),
    ],
)
def test_an_undeterminable_checkout_is_blocked_not_assumed_safe(why: str, path: str) -> None:
    """MEASURED regression: this returned rc=2 before the fix.

    rc=2 means "pip ran but the package is not importable" — so the guard had
    already been passed and the system-wide install attempted. Whatever prevents
    `git rev-parse` from answering (a non-repo, or the documented dubious-
    ownership refusal), the guard must refuse rather than infer safety.
    """
    res = _call(path)

    assert res.returncode == RC_BLOCKED, (
        f"{why}: expected BLOCKED, got rc={res.returncode} — the guard was passed"
    )
    assert "cannot determine" in res.stdout.lower()


def test_dubious_ownership_refusal_is_blocked(tmp_path: Path) -> None:
    """The realistic trigger, simulated through git's own refusal mechanism.

    `safe.directory` is what makes rev-parse fail in practice — it fires when the
    installer's user differs from the repo owner. Rather than manufacture an
    ownership mismatch (not possible unprivileged), point git at a config that
    refuses, which produces the same rev-parse failure the guard must survive.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)

    script = (
        f'source "{_LIB}"; '
        f'GIT_CONFIG_GLOBAL=/dev/null GIT_CEILING_DIRECTORIES="{tmp_path}" '
        f'editable_install_guarded "{tmp_path / "not-a-repo"}" /nonexistent-venv'
    )
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)

    assert res.returncode == RC_BLOCKED, res.stdout + res.stderr


def test_the_documented_return_contract_matches_the_code() -> None:
    """The header documents 0/1/2 and callers branch on those exactly.

    Both callers map anything non-zero-and-not-1 onto "pip ran but Genesis is
    not importable", so a code outside the contract is silently mis-reported —
    which is what an unguarded `set -e` abort (128) used to do.
    """
    text = _LIB.read_text()
    header = text[: text.index("editable_install_guarded() {")]

    assert "1 — blocked" in header
    assert "could not tell" in header, "the undeterminable case must be documented"


@pytest.mark.parametrize("enabled", [False, True])
def test_analytics_extra_is_installed_only_when_opted_in(tmp_path, enabled):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    calls = tmp_path / "pip-calls"
    pip = venv / "bin/pip"
    pip.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\n')
    python = venv / "bin/python"
    python.write_text(
        f'#!/bin/sh\nif [ "$1" = "-m" ]; then\n  exit {0 if enabled else 1}\nfi\nexit 0\n'
    )
    pip.chmod(0o700)
    python.chmod(0o700)
    result = _call(str(repo), str(venv))
    assert result.returncode == 0, result.stderr
    assert ("[transcript-analytics]" in calls.read_text()) is enabled


def _analytics_venv(tmp_path, config_rc=0, import_rc=0, extra_rc=0):
    """Fake only external Python/pip; execute the real shared shell helpers."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    calls = tmp_path / "calls"
    python = venv / "bin/python"
    python.write_text(
        '#!/bin/bash\n'
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        'if [[ "$1" == "-m" ]]; then\n'
        '  [[ "$3" == "--configured-enabled" ]] || exit 99\n'
        f'  exit {config_rc}\nfi\n'
        f'if [[ "$2" == *duckdb* ]]; then exit {import_rc}; fi\nexit 0\n'
    )
    pip = venv / "bin/pip"
    pip.write_text(
        '#!/bin/bash\n'
        f'printf "%s\\n" "$*" >> "{calls}"\n'
        f'if [[ "$*" == *"[transcript-analytics]"* ]]; then exit {extra_rc}; fi\nexit 0\n'
    )
    python.chmod(0o700)
    pip.chmod(0o700)
    return venv, calls


@pytest.mark.parametrize("config_rc,extra_rc", [(0, 1), (2, 0)])
def test_optional_analytics_failure_preserves_core_install(tmp_path, config_rc, extra_rc):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    venv, calls = _analytics_venv(tmp_path, config_rc=config_rc, extra_rc=extra_rc)
    result = _call(str(repo), str(venv))
    assert result.returncode == RC_OK, result.stderr
    assert "WARNING" in result.stderr
    assert ("[transcript-analytics]" in calls.read_text()) is (config_rc == 0)


@pytest.mark.parametrize("script_name", ["bootstrap.sh", "install.sh"])
@pytest.mark.parametrize("config_rc,import_rc,ready", [(0, 0, True), (0, 1, False), (1, 0, False), (2, 0, False)])
def test_real_installer_timer_loop_requires_config_and_imports(
    tmp_path, script_name, config_rc, import_rc, ready
):
    venv, calls = _analytics_venv(tmp_path, config_rc=config_rc, import_rc=import_rc)
    templates = tmp_path / "templates"
    units = tmp_path / "units"
    templates.mkdir()
    units.mkdir()
    for name in ("genesis-transcript-analytics.timer", "genesis-watchdog.timer"):
        (templates / f"{name}.template").touch()
        (units / name).touch()
    body = (_REPO_ROOT / "scripts" / script_name).read_text()
    start = body.index('for template in "$SYSTEMD_TEMPLATE_DIR"/*.timer.template; do')
    end = body.index("\n    done", start)
    loop = body[start:end] + '\n    done\n'
    script = f'''
set -e
source "{_LIB}"
GENESIS_ROOT="{tmp_path}"
VENV_PATH="{venv}"
SYSTEMD_TEMPLATE_DIR="{templates}"
SYSTEMD_USER_DIR="{units}"
GENESIS_TRANSCRIPT_ANALYTICS_DISABLED=1
export GENESIS_TRANSCRIPT_ANALYTICS_DISABLED
systemctl() {{ printf '%s\\n' "$*"; }}
{loop.replace('$GENESIS_ROOT/.venv', str(venv))}
'''
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "enable --now genesis-watchdog.timer" in result.stdout
    assert ("enable --now genesis-transcript-analytics.timer" in result.stdout) is ready
    assert ("disable --now genesis-transcript-analytics.timer" in result.stdout) is not ready
    assert ("import duckdb" in calls.read_text()) is (config_rc == 0)
    if config_rc == 2 or import_rc == 1:
        assert "WARNING" in result.stderr
