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

import os
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


def _fake_venv(tmp_path: Path) -> tuple[Path, Path]:
    """A venv whose pip records its arguments and whose python imports anything."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    log = tmp_path / "pip.args"
    (venv / "bin" / "pip").write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{log}"\n')
    (venv / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")
    for name in ("pip", "python"):
        (venv / "bin" / name).chmod(0o755)
    return venv, log


@pytest.mark.parametrize(("extras", "target_suffix"), [("", ""), ("browser", "[browser]")])
def test_extras_name_the_install_target(tmp_path: Path, extras: str, target_suffix: str) -> None:
    """bootstrap.sh and install.sh install the core packages first, then the
    `browser` extra as a second, non-fatal call through the same guard."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    venv, log = _fake_venv(tmp_path)
    arg = f' "{extras}"' if extras else ""
    script = f'source "{_LIB}"; editable_install_guarded "{repo}" "{venv}"{arg}'
    res = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=120)
    assert res.returncode == RC_OK, res.stdout + res.stderr
    assert log.read_text().splitlines()[:3] == ["install", "-e", f"{repo}{target_suffix}"]


def _recording_venv(tmp_path: Path, *, check_rc: int = 0) -> tuple[Path, Path]:
    """A venv whose pip and python append their arguments to one log; python
    exits ``check_rc`` for `check-packages` and 0 otherwise."""
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    log = tmp_path / "calls"
    (venv / "bin" / "pip").write_text(f'#!/bin/sh\necho "pip $*" >> "{log}"\n')
    (venv / "bin" / "python").write_text(
        "#!/bin/sh\n"
        f'echo "python $*" >> "{log}"\n'
        f'case "$*" in *check-packages*) echo "browser-stack: patchright is not installed"; '
        f"exit {check_rc};; esac\nexit 0\n"
    )
    for name in ("pip", "python"):
        (venv / "bin" / name).chmod(0o755)
    return venv, log


def _install_browser_extra(
    tmp_path: Path, venv: Path, env_extra: dict | None = None
) -> subprocess.CompletedProcess:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    _git("init", "-q", cwd=repo)
    script = f'set -euo pipefail; source "{_LIB}"; install_browser_extra "{repo}" "{venv}"'
    env = {**os.environ, "GENESIS_HOME": str(tmp_path / "gh"), **(env_extra or {})}
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=120, env=env
    )


def test_browser_extra_marks_the_cache_then_installs_then_checks(tmp_path: Path) -> None:
    """mark-legacy before pip: once camoufox 0.5 is installed, its fetch deletes a
    cache holding a pre-0.5 engine that has no .0.5_FLAG (__main__.py:259-263)."""
    venv, log = _recording_venv(tmp_path)
    res = _install_browser_extra(tmp_path, venv)
    assert res.returncode == 0, res.stdout + res.stderr
    calls = log.read_text().splitlines()
    assert calls[0] == "python -m genesis.browser.provision mark-legacy"
    assert calls[1].startswith(f"pip install -e {tmp_path / 'repo'}[browser]")
    # The checkout's pyproject.toml, not the installed metadata pip may have left stale.
    assert calls[-1] == (
        f"python -m genesis.browser.provision check-packages "
        f"--pyproject {tmp_path / 'repo'}/pyproject.toml"
    )


def test_browser_extra_reports_what_pip_left_out(tmp_path: Path) -> None:
    """pip's status is masked and the core import check passes regardless, so the
    package check is what makes a failed browser install visible."""
    venv, _ = _recording_venv(tmp_path, check_rc=1)
    res = _install_browser_extra(tmp_path, venv)
    assert res.returncode == 1
    assert "patchright is not installed" in res.stdout


def test_browser_extra_waits_for_a_running_provisioner(tmp_path: Path) -> None:
    """pip replacing camoufox/playwright/patchright under a running browser-stack
    unit can mix pins mid-install: the upgrade takes the provisioner's lock."""
    import fcntl

    venv, log = _recording_venv(tmp_path)
    lock = tmp_path / "gh" / "locks" / "browser-stack.lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res = _install_browser_extra(
            tmp_path, venv, env_extra={"GENESIS_BROWSER_STACK_WAIT_S": "1"}
        )
    assert res.returncode != 0
    assert not any(c.startswith("pip install") for c in log.read_text().splitlines())
    assert "browser-stack" in res.stdout + res.stderr
