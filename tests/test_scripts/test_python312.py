"""A new venv gets Python 3.12 where the host's own python3 is newer than CI tests.

The installer's interpreter choice runs from the real text of install.sh and
bootstrap.sh, against stub tools, because neither script can run whole here.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "python312.sh"
INSTALL = REPO_ROOT / "scripts" / "install.sh"
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
_REAL_PYTHON = sys.executable


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)
    return path


def _fake_python(path: Path, minor: int) -> Path:
    """An interpreter reporting 3.<minor> that still runs the caller's own check."""
    return _stub(
        path,
        f"""case "$1" in
    --version) echo "Python 3.{minor}.0" ;;
    -c) exec {_REAL_PYTHON} -c "${{2//'sys.version_info[:2]'/(3, {minor})}}" ;;
esac""",
    )


class _Host:
    """A scratch HOME and a PATH holding only what a test puts on it."""

    def __init__(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.bin = tmp_path / "bin"
        self.calls = tmp_path / "calls.log"
        self.home.mkdir()
        self.bin.mkdir()
        self.uv_python = tmp_path / "uv-managed" / "bin" / "python3.12"
        _fake_python(self.uv_python, 12)

    def fake_uv(
        self,
        path: Path,
        *,
        install_rc: int = 0,
        find: str | None = None,
    ) -> Path:
        answer = self.uv_python if find is None else find
        return _stub(
            path,
            f"""echo "uv $*" >> {self.calls}
case "$2" in
    install) exit {install_rc} ;;
    find) echo "{answer}" ;;
esac""",
        )

    def fake_curl(self) -> None:
        """Fetches an installer that drops a uv at the place the real one does."""
        uv_body = self.fake_uv(self.bin.parent / "uv-to-install").read_text()
        _stub(
            self.bin / "curl",
            f"""echo "curl $*" >> {self.calls}
while [ $# -gt 0 ]; do
    if [ "$1" = -o ]; then
        cat > "$2" <<'INSTALLER'
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/uv" <<'UV'
{uv_body}
UV
chmod +x "$HOME/.local/bin/uv"
INSTALLER
    fi
    shift
done""",
        )

    def fake_sudo(self, *, works: bool) -> None:
        _stub(self.bin / "sudo", f'echo "sudo $*" >> {self.calls}\n[ "{works}" = True ]')

    def run(self, script: str) -> subprocess.CompletedProcess[str]:
        for tool in ("ln", "rm", "mkdir", "mktemp", "cat", "chmod", "grep", "sh", "readlink"):
            link = self.bin / tool
            if not link.exists():
                link.symlink_to(f"/usr/bin/{tool}")
        return subprocess.run(
            ["/bin/bash", "-c", script],
            capture_output=True,
            text=True,
            timeout=30,
            env={
                "HOME": str(self.home),
                "PATH": str(self.bin),
                "TMPDIR": str(self.home),
            },
        )

    def logged(self) -> str:
        return self.calls.read_text() if self.calls.exists() else ""


@pytest.fixture
def host(tmp_path: Path) -> _Host:
    return _Host(tmp_path)


def _wants(host: _Host, venv: Path, python: Path) -> bool:
    done = host.run(f'. "{LIB}"; genesis_new_venv_wants_python312 "{venv}" "{python}"')
    assert done.returncode in (0, 1), done.stderr
    return done.returncode == 0


@pytest.mark.parametrize(("minor", "wanted"), [(12, False), (13, True), (14, True)])
def test_only_a_python_newer_than_ci_tests_asks_for_312(host, tmp_path, minor, wanted):
    python = _fake_python(tmp_path / "python3", minor)
    assert _wants(host, tmp_path / "no-venv", python) is wanted


def test_the_real_interpreter_answers_the_same_check(host, tmp_path):
    """The stub above rewrites the check's text; this one runs it unaltered."""
    assert _wants(host, tmp_path / "no-venv", Path(_REAL_PYTHON)) is (
        sys.version_info[:2] > (3, 12)
    )


def test_a_host_already_running_a_venv_is_left_on_it(host, tmp_path):
    """bootstrap.sh runs on every update, so this is what keeps an update from
    installing an interpreter nothing will use."""
    python = _fake_python(tmp_path / "python3", 14)
    venv = tmp_path / "venv"
    _stub(venv / "bin" / "python", "")
    _stub(venv / "bin" / "pip", "")
    assert _wants(host, venv, python) is False


@pytest.mark.parametrize("missing", ["python", "pip"])
def test_a_venv_about_to_be_recreated_counts_as_new(host, tmp_path, missing):
    python = _fake_python(tmp_path / "python3", 14)
    venv = tmp_path / "venv"
    for name in {"python", "pip"} - {missing}:
        _stub(venv / "bin" / name, "")
    assert _wants(host, venv, python) is True


def _install_uv(host: _Host) -> subprocess.CompletedProcess[str]:
    return host.run(f'. "{LIB}"; genesis_install_python312_uv && python3.12 --version')


def test_an_installed_uv_is_used_and_nothing_is_downloaded(host):
    host.fake_uv(host.bin / "uv")
    host.fake_sudo(works=False)
    host.fake_curl()
    done = _install_uv(host)
    assert done.returncode == 0, done.stderr
    assert "Python 3.12" in done.stdout
    assert "curl" not in host.logged()


def test_uv_is_fetched_to_a_file_before_it_runs(host):
    host.fake_sudo(works=False)
    host.fake_curl()
    done = _install_uv(host)
    assert done.returncode == 0, done.stderr
    assert " -o " in host.logged().splitlines()[0]
    assert (host.home / ".local/bin/uv").is_file()


def test_find_skips_a_venv_in_the_working_directory(host):
    """Measured on uv 0.13.7: bare `uv python find 3.12` answers with ./.venv."""
    host.fake_uv(host.bin / "uv")
    host.fake_sudo(works=False)
    assert _install_uv(host).returncode == 0
    assert "uv python find --system 3.12" in host.logged()


def test_a_host_without_sudo_links_it_in_the_users_bin(host):
    host.fake_uv(host.bin / "uv")
    host.fake_sudo(works=False)
    assert _install_uv(host).returncode == 0
    assert (host.home / ".local/bin/python3.12").resolve() == host.uv_python.resolve()


def test_a_failed_download_is_not_run(host):
    """A transfer that drops mid-stream leaves a partial script behind."""
    host.fake_sudo(works=False)
    marker = host.home / "installer-ran"
    _stub(
        host.bin / "curl",
        f"""while [ $# -gt 0 ]; do
    [ "$1" = -o ] && echo ': > {marker}' > "$2"
    shift
done
exit 22""",
    )
    assert _install_uv(host).returncode != 0
    assert not marker.exists()


def test_the_downloaded_installer_is_cleaned_up(host):
    host.fake_sudo(works=False)
    host.fake_curl()
    before = set(host.home.iterdir())
    assert _install_uv(host).returncode == 0
    assert {p.name for p in set(host.home.iterdir()) - before} == {".local"}


@pytest.mark.parametrize("variable", ["XDG_BIN_HOME", "UV_INSTALL_DIR"])
def test_an_installer_told_where_to_put_uv_is_followed(host, tmp_path, variable):
    elsewhere = tmp_path / "elsewhere"
    host.fake_sudo(works=False)
    uv = host.fake_uv(tmp_path / "uv-to-install").read_text()
    _stub(
        host.bin / "curl",
        f"""while [ $# -gt 0 ]; do
    [ "$1" = -o ] && cat > "$2" <<'INSTALLER'
mkdir -p "{elsewhere}"
cat > "{elsewhere}/uv" <<'UV'
{uv}
UV
chmod +x "{elsewhere}/uv"
INSTALLER
    shift
done""",
    )
    done = host.run(f'export {variable}="{elsewhere}"; . "{LIB}"; genesis_install_python312_uv')
    assert done.returncode == 0, done.stderr


def test_sudo_is_asked_without_a_prompt_and_links_the_uv_python(host):
    """Preflight is no place for a password prompt, and this link is a nicety."""
    host.fake_uv(host.bin / "uv")
    _stub(host.bin / "sudo", f'echo "sudo $*" >> {host.calls}')
    _stub(host.bin / "python3.12", "")
    assert _install_uv(host).returncode == 0
    assert f"sudo -n ln -sf {host.uv_python} /usr/local/bin/python3.12" in host.logged()


def test_a_host_where_no_link_can_be_made_still_builds_on_312(entry, host):
    """uv's own path is what the venv is built from, so a link that cannot be
    made is no reason to warn about, or fall back to, the newer Python."""
    _fake_python(host.bin / "python3", 14)
    host.fake_uv(host.bin / "uv")
    host.fake_sudo(works=False)
    _stub(host.bin / "ln", "exit 1")
    done = entry()
    assert done.returncode == 0, done.stderr
    assert "which CI does not test" not in done.stdout
    assert f"CHOSE {host.uv_python}\n" in done.stdout


def test_a_failed_uv_install_links_nothing(host):
    host.fake_uv(host.bin / "uv", install_rc=1)
    host.fake_sudo(works=True)
    assert _install_uv(host).returncode != 0
    assert "sudo" not in host.logged()
    assert not (host.home / ".local/bin/python3.12").exists()


def test_uv_naming_no_interpreter_links_nothing(host):
    host.fake_uv(host.bin / "uv", find="")
    host.fake_sudo(works=True)
    assert _install_uv(host).returncode != 0
    assert "sudo" not in host.logged()


def _block(script: Path, first: str, last: str) -> str:
    """The script's own text from one line up to another, with its helpers."""
    text = script.read_text()
    start, end = text.index(first), text.index(last, text.index(first))
    return text[start:end]


def _install_block() -> str:
    return _block(
        INSTALL,
        "# Python 3.12-3.14 — prefer explicit python3.12 binary",
        'else\n    echo "    python3 not found — installing..."',
    )


def _run_install_block(
    host: _Host, venv: Path, preflight_ok: int = 1
) -> subprocess.CompletedProcess[str]:
    script = (
        "set -euo pipefail\n"
        f'SCRIPT_DIR="{LIB.parent.parent}"\n'
        f'VENV_PATH="{venv}"\n'
        f"PREFLIGHT_OK={preflight_ok}\n"
        'setup_warn() { echo "SETUP_WARN: $1"; }\n' + _install_block() + "fi\n"
        + re.search(r"^PYTHON_BIN=.*\n", INSTALL.read_text(), re.M).group()
        + 'echo "CHOSE $PYTHON_BIN"\n'
    )
    return host.run(script)


def _run_bootstrap_block(host: _Host, root: Path) -> subprocess.CompletedProcess[str]:
    script = (
        "set -euo pipefail\n"
        f'SCRIPT_DIR="{LIB.parent.parent}"\n'
        f'GENESIS_ROOT="{root}"\n'
        f'GENESIS_DEPLOY_SUMMARY="{host.home}/summary"\n'
        f'install_pkg() {{ echo "install_pkg $*" >> {host.calls}; }}\n'
        + _block(
            BOOTSTRAP,
            "# Python 3.12-3.14 required (upper bound mirrors",
            'PYTHON_VERSION=$("$PYTHON_BIN" -c',
        )
        + 'echo "CHOSE $PYTHON_BIN"\n'
    )
    return host.run(script)


@pytest.fixture(params=["install", "bootstrap"])
def entry(request, host, tmp_path):
    """Both ways onto a fresh machine: SETUP.md's quick start uses bootstrap.sh."""
    if request.param == "install":
        return lambda: _run_install_block(host, tmp_path / "root" / ".venv")
    return lambda: _run_bootstrap_block(host, tmp_path / "root")


def test_a_host_on_314_gets_312(entry, host):
    _fake_python(host.bin / "python3", 14)
    host.fake_uv(host.bin / "uv")
    host.fake_sudo(works=False)
    done = entry()
    assert done.returncode == 0, done.stderr
    assert f"CHOSE {host.uv_python}\n" in done.stdout


def test_an_update_on_a_running_venv_installs_nothing(entry, host, tmp_path):
    """Both callers name the venv this run would otherwise create. An update
    goes through bootstrap.sh, so this holds on every update of a 3.14 host."""
    _stub(tmp_path / "root" / ".venv" / "bin" / "python", "")
    _stub(tmp_path / "root" / ".venv" / "bin" / "pip", "")
    _fake_python(host.bin / "python3", 14)
    host.fake_uv(host.bin / "uv")
    host.fake_sudo(works=True)
    host.fake_curl()
    done = entry()
    assert done.returncode == 0, done.stderr
    assert host.logged() == ""
    assert re.search(r"CHOSE .*/python3$", done.stdout, re.M), done.stdout


def test_a_failed_preflight_downloads_nothing(host, tmp_path):
    """A host already refused for its disk should not get a Python download."""
    _fake_python(host.bin / "python3", 14)
    host.fake_uv(host.bin / "uv")
    host.fake_sudo(works=True)
    done = _run_install_block(host, tmp_path / "root" / ".venv", preflight_ok=0)
    assert done.returncode == 0, done.stderr
    assert host.logged() == ""


def test_bootstrap_fetches_curl_first_when_it_is_missing(host, tmp_path):
    """This block runs before bootstrap's own curl step, and a venv built here
    is never rebuilt, so a late curl would leave the host on 3.14 for good."""
    _fake_python(host.bin / "python3", 14)
    host.fake_sudo(works=False)
    done = _run_bootstrap_block(host, tmp_path / "root")
    assert done.returncode == 0, done.stderr
    assert host.logged().splitlines()[0] == "install_pkg curl"


def test_bootstrap_reports_the_miss_past_updates_tail(host, tmp_path):
    _fake_python(host.bin / "python3", 14)
    host.fake_uv(host.bin / "uv", install_rc=1)
    host.fake_sudo(works=False)
    assert _run_bootstrap_block(host, tmp_path / "root").returncode == 0
    assert "could not be installed" in (host.home / "summary").read_text()


def test_a_host_on_312_changes_nothing(entry, host):
    _fake_python(host.bin / "python3", 12)
    host.fake_uv(host.bin / "uv")
    host.fake_sudo(works=True)
    done = entry()
    assert done.returncode == 0, done.stderr
    assert host.logged() == ""
    assert re.search(r"CHOSE .*/python3$", done.stdout, re.M), done.stdout


def test_no_312_available_still_installs_and_says_so(entry, host):
    """Python 3.14 is within pyproject's range, so this is a warning and not a
    stop: a host that cannot reach uv's download should still come up."""
    _fake_python(host.bin / "python3", 14)
    host.fake_uv(host.bin / "uv", install_rc=1)
    host.fake_sudo(works=False)
    done = entry()
    assert done.returncode == 0, done.stderr
    assert "which CI does not test" in done.stdout
    assert re.search(r"CHOSE .*/python3$", done.stdout, re.M), done.stdout


def test_strict_mode_is_told_when_312_could_not_be_had(host, tmp_path):
    """CI runs the installer strict, so this is what fails the fresh-install job
    if the route stops working."""
    _fake_python(host.bin / "python3", 14)
    host.fake_uv(host.bin / "uv", install_rc=1)
    host.fake_sudo(works=False)
    assert "SETUP_WARN: python3.12 could not" in (
        _run_install_block(host, tmp_path / "root" / ".venv").stdout
    )


def test_a_system_python312_is_taken_without_uv(entry, host):
    _fake_python(host.bin / "python3.12", 12)
    _fake_python(host.bin / "python3", 14)
    host.fake_uv(host.bin / "uv")
    host.fake_sudo(works=True)
    done = entry()
    assert done.returncode == 0, done.stderr
    assert host.logged() == ""
    assert f"CHOSE {host.bin}/python3.12" in done.stdout


def test_the_lib_defines_functions_and_runs_nothing(host):
    assert host.run(f'. "{LIB}"').returncode == 0
    assert host.logged() == ""


def test_every_python_choice_in_install_goes_through_the_helper():
    """A second spelling would build the venv through the link again."""
    assert "command -v python3.12 || command -v python3)" not in re.sub(
        r"\(python3\.12 --version.*", "", INSTALL.read_text()
    )
