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

import pytest

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
# SUDO_FAIL_ALL=1: every call fails, as a sudo that cannot authenticate does.
if [ -n "${SUDO_FAIL_ALL:-}" ]; then exit 1; fi
# SUDO_FAIL_INSPECT_SSHD=1: only the sshd drop-in cannot be classified.
if [ "$1" = "/bin/sh" ] && [ -n "${SUDO_FAIL_INSPECT_SSHD:-}" ] && [[ "${*: -1}" == */00-genesis-clientalive.conf ]]; then exit 1; fi
if [ "$1" = "tee" ] && [[ "$2" == */00-genesis-clientalive.conf ]]; then
    if [ -n "${PARTIAL_WRITE_ONCE_MARKER:-}" ] && [ ! -e "$PARTIAL_WRITE_ONCE_MARKER" ]; then
        : > "$PARTIAL_WRITE_ONCE_MARKER"
        head -c 7 > "$2"
        exit 1
    fi
    if [ -n "${SSHD_RESTORE_FAILURE_MARKER:-}" ] && [ -e "$SSHD_RESTORE_FAILURE_MARKER" ]; then exit 1; fi
fi
if [ "$1" = "rm" ] && [ -n "${SUDO_FAIL_REMOVE:-}" ]; then exit 1; fi
if [ "$1" = "rm" ] && [ -n "${SUDO_NOOP_REMOVE:-}" ]; then exit 0; fi
if [ "$1" = "cat" ] && [ -n "${SSHD_READBACK_FAILURE_MARKER:-}" ] && [ -e "$SSHD_READBACK_FAILURE_MARKER" ]; then exit 1; fi
exec "$@"
"""

_SSHD_STUB = """#!/bin/bash
echo "sshd $*" >> "$CALL_LOG"
case "$1" in
    -t)
        if [ -n "${SSHD_RESTORE_FAILURE_MARKER:-}" ]; then : > "$SSHD_RESTORE_FAILURE_MARKER"; fi
        if [ -n "${SSHD_READBACK_FAILURE_MARKER:-}" ]; then : > "$SSHD_READBACK_FAILURE_MARKER"; fi
        exit "${SSHD_T_RC:-0}" ;;
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


@pytest.mark.parametrize("knob", ["SUDO_FAIL_ALL", "SUDO_FAIL_INSPECT_SSHD"])
def test_a_sudo_that_cannot_inspect_still_stops_bootstrap(tmp_path, knob):
    """A sudo that cannot authenticate now fails at the inspection, before any
    write. That must abort as the failed write always did, not read as a kept file."""
    env = _stage(tmp_path)
    env[knob] = "1"
    result = _run(env)
    journald, sshd = _paths(env)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "bootstrap stops here" in result.stdout
    assert not sshd.exists()
    assert ("sshd -t" in _calls(env)) is False


@pytest.mark.parametrize("existing", ["missing", "legacy", "legacy-trailing", "older-stamp", "taken"])
def test_rejected_sshd_preserves_original_bytes_and_has_no_success_summary(tmp_path, existing):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    journald.parent.mkdir(parents=True)
    journald.write_text(_stamped(JOURNALD))
    originals = {
        "legacy": SSHD.encode(),
        "legacy-trailing": (SSHD + "\n\n").encode(),
        "older-stamp": _stamped("ClientAliveInterval 10\nClientAliveCountMax 4\n").encode(),
        "taken": b"# operator's bytes\nClientAliveInterval 30\n\n",
    }
    if existing != "missing":
        sshd.parent.mkdir(parents=True)
        sshd.write_bytes(originals[existing])
    if existing == "taken":
        env["GENESIS_TAKE_TEMPLATES"] = str(sshd)
        env["GENESIS_MU_BACKUP_DIR"] = str(tmp_path / "backups")
    env["SSHD_T_RC"] = "1"
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    if existing == "missing":
        assert not sshd.exists()
    else:
        assert sshd.read_bytes() == originals[existing]
    summary_path = Path(env["GENESIS_DEPLOY_SUMMARY"])
    summary = summary_path.read_text() if summary_path.exists() else ""
    assert not any(line.startswith(("created-etc ", "updated-etc ", "taken-etc "))
                   and str(sshd) in line for line in summary.splitlines())
    assert "reload" not in _calls(env)


def test_failed_metadata_only_write_retains_effective_legacy_configuration(tmp_path):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    for path, content in ((journald, JOURNALD), (sshd, SSHD)):
        path.parent.mkdir(parents=True)
        path.write_text(content)
    env["SUDO_FAIL_TEE"] = "1"
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert journald.read_bytes() == JOURNALD.encode()
    assert sshd.read_bytes() == SSHD.encode()
    assert "already configured and effective" in result.stdout
    assert "restart" not in _calls(env) and "reload" not in _calls(env)


def test_failed_stamp_retains_effective_legacy_configuration(tmp_path):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    for path, content in ((journald, JOURNALD), (sshd, SSHD)):
        path.parent.mkdir(parents=True)
        path.write_text(content)
    failed_stamper = tmp_path / "failed-stamper.py"
    failed_stamper.write_text("raise SystemExit(1)\n")
    env["_GENESIS_ETC_MU"] = str(failed_stamper)
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert journald.read_bytes() == JOURNALD.encode()
    assert sshd.read_bytes() == SSHD.encode()
    assert "restart" not in _calls(env) and "reload" not in _calls(env)


@pytest.mark.parametrize("existing", ["missing", "legacy", "older-stamp", "taken"])
def test_partial_sshd_write_recovers_original_before_fatal_exit(tmp_path, existing):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    journald.parent.mkdir(parents=True)
    journald.write_text(_stamped(JOURNALD))
    sshd.parent.mkdir(parents=True)
    original = (SSHD if existing == "legacy" else _stamped("ClientAliveInterval 10\n"))
    if existing == "taken":
        original = "# private fixture\nClientAliveInterval 30\n\n"
        env["GENESIS_TAKE_TEMPLATES"] = str(sshd)
        env["GENESIS_MU_BACKUP_DIR"] = str(tmp_path / "backups")
    if existing != "missing":
        sshd.write_text(original)
    env["PARTIAL_WRITE_ONCE_MARKER"] = str(tmp_path / "first-write-failed")
    result = _run(env)
    assert result.returncode != 0
    if existing == "missing":
        assert not sshd.exists()
    else:
        assert sshd.read_bytes() == original.encode()
    assert "sshd -t" not in _calls(env) and "reload" not in _calls(env)


@pytest.mark.parametrize("taken", [False, True])
def test_failed_sshd_restore_retains_and_names_recovery_bytes_without_success(tmp_path, taken):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    journald.parent.mkdir(parents=True)
    journald.write_text(_stamped(JOURNALD))
    sshd.parent.mkdir(parents=True)
    original = SSHD if not taken else "ClientAliveInterval 30\n\n"
    sshd.write_text(original)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env.update(TMPDIR=str(scratch), SSHD_T_RC="1",
               SSHD_RESTORE_FAILURE_MARKER=str(tmp_path / "validation-rejected"))
    if taken:
        env["GENESIS_TAKE_TEMPLATES"] = str(sshd)
        env["GENESIS_MU_BACKUP_DIR"] = str(tmp_path / "backups")
    result = _run(env)
    assert result.returncode != 0
    assert "previous drop-in was put back" not in result.stdout
    assert "drop-in removed, sshd untouched" not in result.stdout
    recovery = [path for base in (scratch, tmp_path / "backups") if base.exists()
                for path in base.rglob("*") if path.is_file() and path.read_bytes() == original.encode()]
    assert recovery and any(str(path) in result.stdout for path in recovery)
    summary_path = Path(env["GENESIS_DEPLOY_SUMMARY"])
    summary = summary_path.read_text() if summary_path.exists() else ""
    assert not any(line.startswith(("created-etc ", "updated-etc ", "taken-etc "))
                   and str(sshd) in line for line in summary.splitlines())
    assert "reload" not in _calls(env)


def test_failed_rejected_new_file_removal_is_fatal_and_has_no_success_summary(tmp_path):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    journald.parent.mkdir(parents=True)
    journald.write_text(_stamped(JOURNALD))
    env.update(SSHD_T_RC="1", SUDO_FAIL_REMOVE="1")
    result = _run(env)
    assert result.returncode != 0
    assert "drop-in removed, sshd untouched" not in result.stdout
    summary_path = Path(env["GENESIS_DEPLOY_SUMMARY"])
    summary = summary_path.read_text() if summary_path.exists() else ""
    assert str(sshd) not in summary


@pytest.mark.parametrize("existing", [False, True])
def test_stamp_failure_without_identical_original_stays_fatal(tmp_path, existing):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    journald.parent.mkdir(parents=True)
    journald.write_text(JOURNALD)
    original = b"ClientAliveInterval 30\n"
    if existing:
        sshd.parent.mkdir(parents=True)
        sshd.write_bytes(original)
    stamper = tmp_path / "failed-stamper.py"
    stamper.write_text("raise SystemExit(1)\n")
    env["_GENESIS_ETC_MU"] = str(stamper)
    result = _run(env)
    assert result.returncode != 0
    assert journald.read_bytes() == JOURNALD.encode()
    assert sshd.read_bytes() == original if existing else not sshd.exists()
    assert "sshd -t" not in _calls(env) and "reload" not in _calls(env)


@pytest.mark.parametrize("failure", ["readback", "noop-remove"])
def test_recovery_requires_proof_not_only_successful_write_or_remove(tmp_path, failure):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    journald.parent.mkdir(parents=True)
    journald.write_text(_stamped(JOURNALD))
    original = SSHD.encode()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    env.update(SSHD_T_RC="1", TMPDIR=str(scratch))
    if failure == "readback":
        sshd.parent.mkdir(parents=True)
        sshd.write_bytes(original)
        env["SSHD_READBACK_FAILURE_MARKER"] = str(tmp_path / "rejected")
    else:
        env["SUDO_NOOP_REMOVE"] = "1"
    result = _run(env)
    assert result.returncode != 0
    assert "previous drop-in was put back" not in result.stdout
    assert "drop-in removed, sshd untouched" not in result.stdout
    if failure == "readback":
        assert sshd.read_bytes() == original
        retained = [p for p in scratch.iterdir() if p.read_bytes() == original]
        assert retained and any(str(p) in result.stdout for p in retained)
    else:
        assert sshd.read_text() == _stamped(SSHD)
    assert "reload" not in _calls(env)


@pytest.mark.parametrize("fatal", [False, True])
def test_sshd_rejection_preserves_prior_unrelated_success_summary(tmp_path, fatal):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    env["SSHD_T_RC"] = "1"
    if fatal:
        env["SUDO_FAIL_REMOVE"] = "1"
    result = _run(env)
    assert (result.returncode != 0) == fatal
    summary = Path(env["GENESIS_DEPLOY_SUMMARY"]).read_text().splitlines()
    assert f"created-etc {journald}" in summary
    assert not any(str(sshd) in line for line in summary)
    assert "systemctl restart systemd-journald" in _calls(env)
    assert "reload" not in _calls(env)


def test_snapshot_creation_failure_keeps_existing_bytes_without_writing(tmp_path):
    env = _stage(tmp_path)
    journald, sshd = _paths(env)
    for target, content in ((journald, JOURNALD), (sshd, SSHD)):
        target.parent.mkdir(parents=True)
        target.write_text(content)
    mktemp = tmp_path / "bin/mktemp"
    mktemp.write_text("#!/bin/sh\nexit 1\n")
    mktemp.chmod(0o755)
    result = _run(env)
    assert result.returncode == 0, result.stdout + result.stderr
    assert journald.read_bytes() == JOURNALD.encode()
    assert sshd.read_bytes() == SSHD.encode()
    assert "could not snapshot" in result.stdout
    assert "sshd -t" not in _calls(env) and "restart" not in _calls(env)
