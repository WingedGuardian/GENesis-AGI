"""bootstrap.sh's journald and sshd drop-in steps (the `etc-dropins` block).

The block is extracted from the REAL bootstrap.sh and run with /etc rewritten to a
scratch root and sudo/sshd/systemctl stubbed on PATH, so the logic under test is
the shipped code: both files are stamped, a file edited by hand is kept and named
rather than overwritten, a pre-stamp file is stamped without restarting anything,
a taken file is backed up, and a sudo that cannot write still stops bootstrap, as
it always did.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO_ROOT / "scripts" / "bootstrap.sh"
SCRIPTS = REPO_ROOT / "scripts"

JOURNALD = "[Journal]\nSystemMaxUse=200M\n"
SSHD = (
    "# Genesis: detect dead SSH clients fast so an orphaned foreground session\n"
    "# cannot keep executing headless (see duplicate-session guard).\n"
    "ClientAliveInterval 15\nClientAliveCountMax 4\n"
)

_SUDO_STUB = """#!/bin/bash
# Passthrough; SUDO_FAIL_TEE=1 makes a write fail, as a sudo that needs a password
# and has no terminal would.
if [ "$1" = "tee" ] && [ -n "${SUDO_FAIL_TEE:-}" ]; then exit 1; fi
exec "$@"
"""

_SSHD_STUB = """#!/bin/bash
echo "sshd $*" >> "$CALL_LOG"
case "$1" in
    -t) exit "${SSHD_T_RC:-0}" ;;
    -T) echo "clientaliveinterval ${SSHD_EFFECTIVE:-15}" ;;
esac
exit 0
"""

_SYSTEMCTL_STUB = """#!/bin/bash
echo "systemctl $*" >> "$CALL_LOG"
exit 0
"""


def _stamped(body: str) -> str:
    body = body.rstrip("\n") + "\n"
    return body + f"# genesis-managed v1 sha256={hashlib.sha256(body.encode()).hexdigest()}\n"


def _block(etc: Path) -> str:
    text = BOOTSTRAP.read_text()
    start = text.index("# BEGIN etc-dropins")
    end = text.index("# END etc-dropins")
    return text[start:end].replace("/etc/", f"{etc}/")


def _stage(tmp_path: Path) -> dict:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("sudo", _SUDO_STUB), ("sshd", _SSHD_STUB), ("systemctl", _SYSTEMCTL_STUB)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(tmp_path / "home"),
        "CALL_LOG": str(tmp_path / "calls.log"),
        "GENESIS_DEPLOY_SUMMARY": str(tmp_path / "summary.txt"),
        "ETC": str(tmp_path / "etc"),
    }


def _run(env: dict) -> subprocess.CompletedProcess:
    script = f'set -euo pipefail\nSCRIPT_DIR="{SCRIPTS}"\n' + _block(Path(env["ETC"]))
    return subprocess.run(
        ["/bin/bash", "-c", script], capture_output=True, text=True, timeout=60, env=env
    )


def _paths(env: dict) -> tuple[Path, Path]:
    etc = Path(env["ETC"])
    return (
        etc / "systemd/journald.conf.d/genesis-size-cap.conf",
        etc / "ssh/sshd_config.d/00-genesis-clientalive.conf",
    )


def _calls(env: dict) -> str:
    log = Path(env["CALL_LOG"])
    return log.read_text() if log.exists() else ""


def test_a_fresh_run_writes_both_stamped_and_applies_them(tmp_path):
    env = _stage(tmp_path)
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    journald, sshd = _paths(env)
    assert journald.read_text() == _stamped(JOURNALD)
    assert sshd.read_text() == _stamped(SSHD)
    calls = _calls(env)
    assert "systemctl restart systemd-journald" in calls
    assert "sshd -t" in calls and "systemctl reload ssh" in calls
    assert "ClientAlive configured" in result.stdout


def test_a_second_run_touches_nothing(tmp_path):
    env = _stage(tmp_path)
    _run(env)
    Path(env["CALL_LOG"]).write_text("")
    result = _run(env)
    assert result.returncode == 0
    assert "Journal cap already set" in result.stdout
    assert "already configured and effective" in result.stdout
    calls = _calls(env)
    assert "restart" not in calls and "reload" not in calls and "sshd -t" not in calls


def test_pre_stamp_files_are_stamped_without_a_restart_or_reload(tmp_path):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    journald.parent.mkdir(parents=True)
    sshd.parent.mkdir(parents=True)
    journald.write_text(JOURNALD)
    sshd.write_text(SSHD)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert journald.read_text() == _stamped(JOURNALD)
    assert sshd.read_text() == _stamped(SSHD)
    calls = _calls(env)
    assert "restart" not in calls and "reload" not in calls
    assert "sshd -t" in calls  # still validated after the rewrite


def test_an_sshd_drop_in_edited_by_hand_is_kept_and_named(tmp_path):
    env = _stage(tmp_path)
    _, sshd = _paths(env)
    sshd.parent.mkdir(parents=True)
    edited = SSHD.replace("ClientAliveInterval 15", "ClientAliveInterval 30")
    sshd.write_text(edited)
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert sshd.read_text() == edited
    assert "was edited by hand" in result.stdout
    assert "sorts before 00-genesis-clientalive.conf" in result.stdout  # first value wins
    assert "reload" not in _calls(env)
    summary = Path(env["GENESIS_DEPLOY_SUMMARY"]).read_text()
    assert f"kept-etc {sshd}: edited by hand" in summary


def test_a_taken_sshd_drop_in_that_sshd_rejects_is_put_back(tmp_path):
    env = _stage(tmp_path)
    _, sshd = _paths(env)
    sshd.parent.mkdir(parents=True)
    mine = "ClientAliveInterval 30\n"
    sshd.write_text(mine)
    env["GENESIS_TAKE_TEMPLATES"] = str(sshd)
    env["GENESIS_MU_BACKUP_DIR"] = str(tmp_path / "backups")
    env["SSHD_T_RC"] = "1"
    result = _run(env)
    assert result.returncode == 0, result.stderr
    assert sshd.read_text() == mine
    assert "previous drop-in was put back" in result.stdout
    assert "reload" not in _calls(env)


def test_a_rejected_own_write_is_removed_as_before(tmp_path):
    env = _stage(tmp_path)
    env["SSHD_T_RC"] = "1"
    result = _run(env)
    assert result.returncode == 0
    assert not _paths(env)[1].exists()
    assert "drop-in removed, sshd untouched" in result.stdout


def test_a_sudo_that_cannot_write_still_stops_bootstrap(tmp_path):
    env = _stage(tmp_path)
    env["SUDO_FAIL_TEE"] = "1"
    result = _run(env)
    assert result.returncode == 1
    assert "bootstrap stops here" in result.stdout
