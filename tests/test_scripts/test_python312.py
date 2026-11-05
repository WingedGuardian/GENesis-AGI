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
PIN = re.search(r'^GENESIS_UV_VERSION="(.+)"$', LIB.read_text(), re.M).group(1)


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/bash\n{body}\n")
    path.chmod(0o755)
    return path


def _fake_python(path: Path, minor: int, patch: int = 0) -> Path:
    """An interpreter reporting 3.<minor>.<patch> that still runs the caller's own check,
    and writes pyvenv.cfg for `-m venv --upgrade` the way a real one does."""
    return _stub(
        path,
        f"""case "$1" in
    --version) echo "Python 3.{minor}.{patch}" ;;
    -c) exec {_REAL_PYTHON} -c "${{2//'sys.version_info[:2]'/(3, {minor})}}" ;;
    -m)
        echo "$0 $*" >> "$GENESIS_TEST_CALLS"
        [ -z "${{GENESIS_TEST_VENV_RC:-}}" ] || {{ echo "venv failed here"; exit "$GENESIS_TEST_VENV_RC"; }}
        for venv; do :; done
        printf 'version = 3.{minor}.{patch}\\nexecutable = %s\\n' "$(readlink -f "$0")" > "$venv/pyvenv.cfg"
        ;;
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
        # Laid out as uv does: a minor-version link to the patch installed now.
        self.managed = tmp_path / "uv-managed"
        for number, patch in enumerate(("p1", "p2"), start=1):
            _fake_python(self.managed / patch / "bin" / "python3.12", 12, number)
        (self.managed / "minor").symlink_to("p1")
        self.uv_python = self.managed / "minor" / "bin" / "python3.12"

    def fake_uv(
        self,
        path: Path,
        *,
        install_rc: int = 0,
        upgrade_rc: int = 0,
        upgrade_moves: bool = False,
        find: str | None = None,
        managed: str | None = None,
        version: str = PIN,
        self_rc: int = 0,
    ) -> Path:
        answer = self.uv_python if find is None else find
        dir_answer = self.managed if managed is None else managed
        return _stub(
            path,
            f"""[ "$1" = --version ] && {{ echo "uv {version} (x86_64-unknown-linux-gnu)"; exit 0; }}
echo "uv $*" >> {self.calls}
[ "$1" = self ] && {{ echo "self update env UV_NO_MODIFY_PATH=${{UV_NO_MODIFY_PATH:-}}" >> {self.calls}; exit {self_rc}; }}
case "$2" in
    install) exit {install_rc} ;;
    find) echo "{answer}" ;;
    dir) echo "{dir_answer}" ;;
    upgrade)
        {f'ln -sfn p2 "{self.managed}/minor"' if upgrade_moves else ":"}
        [ {upgrade_rc} = 0 ] || echo "error: network down"
        exit {upgrade_rc} ;;
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

    def run(self, script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        for tool in ("ln", "rm", "mkdir", "mktemp", "cat", "chmod", "grep", "sh", "readlink", "sed", "sort", "head", "tail", "date", "timeout"):
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
                "GENESIS_TEST_CALLS": str(self.calls),
                **(env or {}),
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


def _uv_venv(host: _Host, venv: Path, *, through: Path | None = None, recorded: Path | None = None) -> Path:
    """A venv on uv's Python, laid out as a real one is: bin/python links to
    bin/python3.12, which links to the interpreter, and pyvenv.cfg names the patch."""
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3.12").symlink_to(through or host.uv_python)
    (venv / "bin" / "python").symlink_to("python3.12")
    (venv / "pyvenv.cfg").write_text(f"version = 3.12.1\nexecutable = {recorded or host.uv_python.resolve()}\n")
    return venv


def _packaged_venv(tmp_path: Path) -> Path:
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(_fake_python(tmp_path / "sys" / "python3.12", 12))
    return venv


_REPORT = "; ".join(
    f'echo "{name}=${name}"'
    for name in (
        "GENESIS_UV_PYTHON_MOVED",
        "GENESIS_UV_MOVED",
        "GENESIS_UV_BEHIND",
        "GENESIS_UV_PIN_STALE",
        "GENESIS_UV_PYTHON_ERROR",
    )
)


def _upgrade(host: _Host, venv: Path, *, setup: str = ":", **env: str) -> subprocess.CompletedProcess[str]:
    return host.run(f'. "{LIB}"; {setup}; genesis_upgrade_uv_python "{venv}"; rc=$?; {_REPORT}; exit $rc', env=env)


def test_a_venv_on_uvs_python_is_upgraded_within_its_minor(host, tmp_path):
    host.fake_uv(host.bin / "uv")
    done = _upgrade(host, _uv_venv(host, tmp_path / "venv"))
    assert done.returncode == 0, done.stderr
    assert "uv python upgrade 3.12" in host.logged()
    assert "GENESIS_UV_PYTHON_MOVED=\n" in done.stdout


def test_uv_below_the_pin_comes_up_to_it_first(host, tmp_path):
    """uv only knows patches released before its own build."""
    host.fake_uv(host.bin / "uv", version="0.1.0")
    done = _upgrade(host, _uv_venv(host, tmp_path / "venv"))
    log = host.logged()
    assert log.index(f"uv self update {PIN}") < log.index("uv python upgrade")
    assert f"GENESIS_UV_MOVED=0.1.0 to {PIN}\n" in done.stdout


def test_bringing_uv_to_the_pin_leaves_shell_files_alone(host, tmp_path):
    """On a default install's receipt uv re-adds its PATH lines to .bashrc and .profile
    when it updates itself, unless told not to (measured). An update has no business
    editing those."""
    host.fake_uv(host.bin / "uv", version="0.1.0")
    _upgrade(host, _uv_venv(host, tmp_path / "venv"))
    assert "UV_NO_MODIFY_PATH=1" in host.logged()


@pytest.mark.parametrize("version", [PIN, "99.0.0"])
def test_uv_at_or_above_the_pin_is_left_alone(host, tmp_path, version):
    """Never down: a newer uv may have been installed for something else."""
    host.fake_uv(host.bin / "uv", version=version)
    done = _upgrade(host, _uv_venv(host, tmp_path / "venv"))
    assert "self update" not in host.logged()
    assert "GENESIS_UV_MOVED=\n" in done.stdout


def test_versions_compare_as_versions_and_not_as_text(host, tmp_path):
    host.fake_uv(host.bin / "uv", version="0.9.0")
    _upgrade(host, _uv_venv(host, tmp_path / "venv"), setup='GENESIS_UV_VERSION=0.13.7')
    assert "self update 0.13.7" in host.logged()


def test_a_uv_that_cannot_update_itself_is_reported_and_still_upgrades(host, tmp_path):
    host.fake_uv(host.bin / "uv", version="0.1.0", self_rc=1)
    done = _upgrade(host, _uv_venv(host, tmp_path / "venv"))
    assert done.returncode == 0, done.stderr
    assert "uv python upgrade" in host.logged()
    assert "GENESIS_UV_BEHIND=0.1.0\n" in done.stdout
    assert "GENESIS_UV_MOVED=\n" in done.stdout


def test_a_pin_past_its_age_says_so(host, tmp_path):
    host.fake_uv(host.bin / "uv")
    venv = _uv_venv(host, tmp_path / "venv")
    assert "GENESIS_UV_PIN_STALE=\n" in _upgrade(host, venv).stdout
    assert "GENESIS_UV_PIN_STALE=1\n" in _upgrade(host, venv, setup="GENESIS_UV_PINNED_ON=2000-01-01").stdout


def test_a_new_install_fetches_the_pinned_uv(host):
    host.fake_sudo(works=False)
    host.fake_curl()
    assert _install_uv(host).returncode == 0
    assert f"https://astral.sh/uv/{PIN}/install.sh" in host.logged()


def test_a_patch_that_moved_is_recorded_without_touching_pip(host, tmp_path):
    """Without the record pyvenv.cfg names the old patch and the graph census reads
    every process as exe_mismatch. Moving pip would leave a rollback fetching one."""
    host.fake_uv(host.bin / "uv", upgrade_moves=True)
    venv = _uv_venv(host, tmp_path / "venv")
    done = _upgrade(host, venv)
    assert done.returncode == 0, done.stderr
    assert f"{host.uv_python} -m venv --upgrade --without-pip {venv}" in host.logged()
    assert "version = 3.12.2\n" in (venv / "pyvenv.cfg").read_text()
    assert "GENESIS_UV_PYTHON_MOVED=Python 3.12.1 to Python 3.12.2\n" in done.stdout


def test_a_venv_built_on_a_versioned_path_is_moved_too(host, tmp_path):
    """It does not follow uv's upgrade, and --upgrade alone rewrites the record while
    the venv keeps running the old binary (measured on real uv)."""
    host.fake_uv(host.bin / "uv", upgrade_moves=True)
    venv = _uv_venv(host, tmp_path / "venv", through=host.managed / "p1" / "bin" / "python3.12")
    done = _upgrade(host, venv)
    assert done.returncode == 0, done.stderr
    assert (venv / "bin" / "python").resolve() == host.managed / "p2" / "bin" / "python3.12"
    assert "GENESIS_UV_PYTHON_MOVED=Python 3.12.1 to Python 3.12.2\n" in done.stdout


def test_a_venv_made_through_python3_is_moved_too(host, tmp_path):
    """`python3 -m venv` puts the absolute link on python3, with python and python3.12
    pointing at it, so re-pointing only python3.12 would change nothing that runs."""
    host.fake_uv(host.bin / "uv", upgrade_moves=True)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").symlink_to(host.managed / "p1" / "bin" / "python3.12")
    (venv / "bin" / "python").symlink_to("python3")
    (venv / "bin" / "python3.12").symlink_to("python3")
    (venv / "pyvenv.cfg").write_text(f"version = 3.12.1\nexecutable = {host.managed / 'p1' / 'bin' / 'python3.12'}\n")
    done = _upgrade(host, venv)
    assert done.returncode == 0, done.stderr
    assert (venv / "bin" / "python").resolve() == host.managed / "p2" / "bin" / "python3.12"
    assert "GENESIS_UV_PYTHON_MOVED=Python 3.12.1 to Python 3.12.2\n" in done.stdout


def test_a_venv_that_cannot_be_re_pointed_is_reported_and_its_record_left_true(host, tmp_path):
    """A relative link out of the venv is not one this rewrites. Writing the new patch
    into the record while the old one runs would be worse than doing nothing."""
    host.fake_uv(host.bin / "uv", upgrade_moves=True)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to("../../uv-managed/p1/bin/python3.12")
    (venv / "pyvenv.cfg").write_text("version = 3.12.1\n")
    done = _upgrade(host, venv)
    assert done.returncode != 0
    assert "still runs" in done.stdout
    assert "-m venv" not in host.logged()
    assert (venv / "pyvenv.cfg").read_text() == "version = 3.12.1\n"


def test_a_stale_version_alone_is_repaired(host, tmp_path):
    """Otherwise the same move is reported on every update."""
    host.fake_uv(host.bin / "uv")
    venv = _uv_venv(host, tmp_path / "venv")
    (venv / "pyvenv.cfg").write_text(f"version = 3.12.0\nexecutable = {host.uv_python.resolve()}\n")
    assert _upgrade(host, venv).returncode == 0
    assert "-m venv --upgrade" in host.logged()
    assert "GENESIS_UV_PYTHON_MOVED=\n" in _upgrade(host, venv).stdout


def test_a_system_site_setting_survives_the_repair(host, tmp_path):
    host.fake_uv(host.bin / "uv", upgrade_moves=True)
    venv = _uv_venv(host, tmp_path / "venv")
    cfg = venv / "pyvenv.cfg"
    cfg.write_text(cfg.read_text() + "include-system-site-packages = true\n")
    assert _upgrade(host, venv).returncode == 0
    assert "--upgrade --without-pip --system-site-packages" in host.logged()


def test_a_uv_that_cannot_name_its_directory_is_reported(host, tmp_path):
    """Doubt, not "nothing to do": on a uv Python this would stop patching silently."""
    uv = host.fake_uv(host.bin / "uv")
    uv.write_text(uv.read_text().replace('case "$2" in', '[ "$2" = dir ] && exit 3\ncase "$2" in'))
    done = _upgrade(host, _uv_venv(host, tmp_path / "venv"))
    assert done.returncode != 0
    assert "uv python dir failed" in done.stdout


def test_uv_calls_that_download_are_bounded(host, tmp_path):
    """The server is stopped while this runs, so a stalled call is an outage."""
    uv = host.fake_uv(host.bin / "uv", version="0.1.0")
    uv.write_text(uv.read_text().replace('case "$2" in', '[ "$2" = upgrade ] && sleep 30\ncase "$2" in'))
    (host.bin / "sleep").symlink_to("/usr/bin/sleep")
    done = _upgrade(host, _uv_venv(host, tmp_path / "venv"), setup="GENESIS_UV_TIMEOUT_S=1")
    assert done.returncode != 0
    assert "GENESIS_UV_PYTHON_ERROR=" in done.stdout


def test_a_venv_python_that_cannot_run_is_reported(host, tmp_path):
    host.fake_uv(host.bin / "uv")
    _stub(host.managed / "broken" / "bin" / "python3.12", "exit 1")
    done = _upgrade(host, _uv_venv(host, tmp_path / "venv", through=host.managed / "broken" / "bin" / "python3.12"))
    assert done.returncode != 0
    assert "could not report its version" in done.stdout
    assert "upgrade" not in host.logged()


def test_a_link_that_cannot_be_rewritten_is_reported(host, tmp_path):
    host.fake_uv(host.bin / "uv", upgrade_moves=True)
    # Only the venv's links: the fake uv moves its patch with ln too.
    _stub(host.bin / "ln", 'case "$3" in */venv/*) exit 1 ;; esac\nexec /usr/bin/ln "$@"')
    venv = _uv_venv(host, tmp_path / "venv", through=host.managed / "p1" / "bin" / "python3.12")
    done = _upgrade(host, venv)
    assert done.returncode != 0
    assert "could not re-point" in done.stdout
    assert "-m venv" not in host.logged()


def test_a_record_left_stale_is_repaired_and_the_move_still_reported(host, tmp_path):
    """Say the last run moved the patch and then failed to write the record. This run
    moves nothing itself, and still has to repair it and tell the reader."""
    host.fake_uv(host.bin / "uv")
    venv = _uv_venv(host, tmp_path / "venv", recorded=host.managed / "p0" / "bin" / "python3.12")
    (venv / "pyvenv.cfg").write_text(
        f"version = 3.12.0\nexecutable = {host.managed / 'p0' / 'bin' / 'python3.12'}\n"
    )
    done = _upgrade(host, venv)
    assert done.returncode == 0, done.stderr
    assert "-m venv --upgrade" in host.logged()
    assert "GENESIS_UV_PYTHON_MOVED=Python 3.12.0 to Python 3.12.1\n" in done.stdout


def test_a_current_record_is_left_alone(host, tmp_path):
    """The usual update: pyvenv.cfg is not rewritten under processes starting up."""
    host.fake_uv(host.bin / "uv")
    assert _upgrade(host, _uv_venv(host, tmp_path / "venv")).returncode == 0
    assert "-m venv" not in host.logged()


def test_a_failed_repair_names_the_reason_and_is_not_called_a_move(host, tmp_path):
    host.fake_uv(host.bin / "uv", upgrade_moves=True)
    done = _upgrade(host, _uv_venv(host, tmp_path / "venv"), GENESIS_TEST_VENV_RC="1")
    assert done.returncode != 0
    assert "GENESIS_UV_PYTHON_ERROR=venv failed here\n" in done.stdout
    assert "GENESIS_UV_PYTHON_MOVED=\n" in done.stdout


def test_find_asks_for_a_python_uv_manages(host, tmp_path):
    """A uv.toml preferring the system makes plain `find --system` answer with it, and
    `upgrade` still moved the venv: a refusal here would stay permanent (measured)."""
    host.fake_uv(host.bin / "uv")
    _upgrade(host, _uv_venv(host, tmp_path / "venv"))
    assert "uv python find --managed-python --system 3.12" in host.logged()


def test_a_python_uv_does_not_manage_is_never_written_into_the_record(host, tmp_path):
    system = _fake_python(tmp_path / "usr" / "python3.12", 12)
    host.fake_uv(host.bin / "uv", upgrade_moves=True, find=str(system))
    done = _upgrade(host, _uv_venv(host, tmp_path / "venv"))
    assert done.returncode != 0
    assert "does not manage" in done.stdout
    assert "-m venv" not in host.logged()


def test_a_venv_on_a_packaged_python_is_not_touched(host, tmp_path):
    """On those hosts the package manager patches it, so uv changes nothing."""
    host.fake_uv(host.bin / "uv", version="0.1.0")
    assert _upgrade(host, _packaged_venv(tmp_path)).returncode == 0
    assert "self update" not in host.logged()
    assert "upgrade" not in host.logged()


@pytest.mark.parametrize("answer", ["", "/nonexistent/parent/uv/python"])
def test_a_managed_directory_that_resolves_to_nothing_touches_nothing(host, tmp_path, answer):
    """Either answer canonicalises to empty, which would make every path look managed.
    Measured on real uv: this installed a Python on a host with a packaged one."""
    host.fake_uv(host.bin / "uv", managed=answer)
    assert _upgrade(host, _packaged_venv(tmp_path)).returncode == 0
    assert "upgrade" not in host.logged()


def test_a_home_reached_through_a_symlink_still_counts_as_managed(host, tmp_path):
    """uv does not canonicalise its answer, and readlink -f does."""
    alias = tmp_path / "managed-alias"
    alias.symlink_to(host.managed)
    host.fake_uv(host.bin / "uv", managed=str(alias))
    assert _upgrade(host, _uv_venv(host, tmp_path / "venv")).returncode == 0
    assert "uv python upgrade" in host.logged()


def test_no_venv_or_no_uv_is_nothing_to_do(host, tmp_path):
    host.fake_uv(tmp_path / "not-on-path" / "uv")
    assert _upgrade(host, _uv_venv(host, tmp_path / "venv")).returncode == 0
    host.fake_uv(host.bin / "uv")
    assert _upgrade(host, tmp_path / "absent").returncode == 0
    assert host.logged() == ""


def test_a_failed_upgrade_names_uvs_reason(host, tmp_path):
    host.fake_uv(host.bin / "uv", upgrade_rc=1)
    done = _upgrade(host, _uv_venv(host, tmp_path / "venv"))
    assert done.returncode != 0
    assert "GENESIS_UV_PYTHON_ERROR=error: network down\n" in done.stdout


def _run_bootstrap_venv_block(host: _Host, root: Path, summary: str = "") -> subprocess.CompletedProcess[str]:
    script = (
        "set -euo pipefail\n"
        f'GENESIS_ROOT="{root}"\n'
        f'GENESIS_DEPLOY_SUMMARY="{summary or host.home / "summary"}"\n'
        f'. "{LIB}"\n'
        "PYTHON_BIN=/nonexistent\n"
        + _block(BOOTSTRAP, "# --- Python venv ---", "# Worktree guard + editable install")
        + 'echo "REACHED THE INSTALL"\n'
    )
    return host.run(script)


def _running_venv(host: _Host, root: Path) -> None:
    _stub(_uv_venv(host, root / ".venv") / "bin" / "pip", "")


def test_bootstrap_says_so_past_updates_tail_when_the_patch_moved(host, tmp_path):
    _running_venv(host, tmp_path / "root")
    host.fake_uv(host.bin / "uv", upgrade_moves=True)
    done = _run_bootstrap_venv_block(host, tmp_path / "root")
    assert done.returncode == 0, done.stderr
    assert "REACHED THE INSTALL" in done.stdout
    summary = (host.home / "summary").read_text()
    assert "Python 3.12.1 to Python 3.12.2" in summary
    assert "cannot use the payment vault" in summary


def test_bootstrap_says_when_uv_moved_or_could_not(host, tmp_path):
    _running_venv(host, tmp_path / "root")
    host.fake_uv(host.bin / "uv", version="0.1.0")
    assert _run_bootstrap_venv_block(host, tmp_path / "root").returncode == 0
    assert "uv moved to the pinned version" in (host.home / "summary").read_text()
    (host.home / "summary").unlink()
    host.fake_uv(host.bin / "uv", version="0.1.0", self_rc=1)
    assert _run_bootstrap_venv_block(host, tmp_path / "root").returncode == 0
    assert "could not update itself" in (host.home / "summary").read_text()


def test_bootstrap_says_when_the_pin_has_aged(host, tmp_path):
    _running_venv(host, tmp_path / "root")
    host.fake_uv(host.bin / "uv")
    text = _block(BOOTSTRAP, "# --- Python venv ---", "# Worktree guard + editable install")
    script = (
        "set -euo pipefail\n"
        f'GENESIS_ROOT="{tmp_path / "root"}"\n'
        f'GENESIS_DEPLOY_SUMMARY="{host.home / "summary"}"\n'
        f'. "{LIB}"\nGENESIS_UV_PINNED_ON=2000-01-01\nPYTHON_BIN=/nonexistent\n' + text
    )
    assert host.run(script).returncode == 0
    assert "Bump it in scripts/lib/python312.sh" in (host.home / "summary").read_text()


def test_bootstrap_stays_quiet_when_nothing_moved(host, tmp_path):
    _running_venv(host, tmp_path / "root")
    host.fake_uv(host.bin / "uv")
    done = _run_bootstrap_venv_block(host, tmp_path / "root")
    assert done.returncode == 0, done.stderr
    assert not (host.home / "summary").exists()


def test_a_failed_upgrade_does_not_stop_the_update(host, tmp_path):
    """An old patch still works; an update stopped here applies nothing else."""
    _running_venv(host, tmp_path / "root")
    host.fake_uv(host.bin / "uv", upgrade_rc=1)
    done = _run_bootstrap_venv_block(host, tmp_path / "root")
    assert done.returncode == 0, done.stderr
    assert "REACHED THE INSTALL" in done.stdout
    summary = (host.home / "summary").read_text()
    assert "error: network down" in summary
    assert "the next update tries again" in summary


@pytest.mark.parametrize("failure", ["upgrade fails", "patch moved", "uv moved", "uv behind"])
def test_a_full_disk_writing_the_summary_does_not_stop_the_update(host, tmp_path, failure):
    """A full disk is also a likely reason for the upgrade to have failed."""
    _running_venv(host, tmp_path / "root")
    host.fake_uv(
        host.bin / "uv",
        upgrade_rc=int(failure == "upgrade fails"),
        upgrade_moves=failure == "patch moved",
        version="0.1.0" if failure.startswith("uv") else PIN,
        self_rc=int(failure == "uv behind"),
    )
    done = _run_bootstrap_venv_block(host, tmp_path / "root", summary="/dev/full")
    assert done.returncode == 0, done.stderr
    assert "REACHED THE INSTALL" in done.stdout
