"""Behavioral tests for cc_tmp_volume_apply / cc_tmp_volume_remove
(scripts/lib/cc_tmp_volume.sh).

The functions are sourced from the REAL lib and driven under the callers'
``set -euo pipefail`` (host-setup.sh and the gateway redeploy verb both run that
way), with a stubbed ``incus`` on PATH that logs every invocation to
``$INCUS_LOG`` and answers from ``INCUS_*`` env vars. A ``__DONE__`` sentinel
printed after the call proves the function returned instead of tripping errexit
— every guard/degrade path must never abort an install or a redeploy.

Design facts encoded here were spike-proven on the real host (2026-07-18):
attach hot-plugs live; a fresh volume needs ``chown`` from container-root (no
security.shifted); IO limits are set one key per call; a full volume leaves the
rootfs untouched.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LIB = REPO_ROOT / "scripts" / "lib" / "cc_tmp_volume.sh"
HOST_SETUP = REPO_ROOT / "scripts" / "host-setup.sh"
GATEWAY = REPO_ROOT / "scripts" / "guardian-gateway.sh"
UPDATE_SH = REPO_ROOT / "scripts" / "update.sh"
WATCHDOG = REPO_ROOT / "src" / "genesis" / "guardian" / "watchdog.py"
DEPLOY_HEALTH = REPO_ROOT / "src" / "genesis" / "observability" / "snapshots" / "deploy_health.py"

# A single stub `incus` covering every subcommand the lib calls. Behavior is
# driven by INCUS_* env vars; every invocation is appended to $INCUS_LOG so
# tests can assert ordering AND absence ("no create when a session is live").
_INCUS_STUB = r"""#!/bin/bash
echo "$*" >> "$INCUS_LOG"
case "$1" in
  info)
    [ "${INCUS_INFO_MISSING:-0}" = "1" ] && exit 1
    echo "Status: ${INCUS_INFO_STATUS:-Running}"
    exit 0 ;;
  config)
    if [ "$2 $3" = "device get" ]; then
      dev="$5"; key="$6"
      if [ "$dev" = "root" ]; then
        case "$key" in
          pool) echo "${INCUS_POOL:-default}" ;;
          limits.read) echo "${INCUS_LIMIT_READ-190MB}" ;;
          limits.write) echo "${INCUS_LIMIT_WRITE-90MB}" ;;
        esac
        exit 0
      fi
      if [ "$key" = "source" ]; then
        [ "${INCUS_DEVICE_ATTACHED:-0}" = "1" ] && { echo "${INCUS_VOL:-vol}"; exit 0; }
        exit 1
      fi
      exit 0
    fi
    [ "$2 $3" = "device add" ] && exit "${INCUS_ATTACH_RC:-0}"
    exit 0 ;;
  storage)
    if [ "$2" = "list" ]; then echo "default,lvm,vg0,,1,CREATED"; exit 0; fi
    if [ "$2" = "show" ]; then echo "driver: ${INCUS_DRIVER:-lvm}"; exit 0; fi
    if [ "$2 $3" = "volume show" ]; then exit "${INCUS_VOLUME_EXISTS:-1}"; fi
    if [ "$2 $3" = "volume create" ]; then exit "${INCUS_CREATE_RC:-0}"; fi
    exit 0 ;;
  exec)
    inner=""; seen=0
    for a in "$@"; do
      if [ "$seen" = "1" ]; then inner="$a"; break; fi
      [ "$a" = "--" ] && seen=1
    done
    case "$inner" in
      pgrep)
        # count form (-c / -xc) prints a number like real `pgrep -c`; the plain
        # existence form (-x) just sets exit status. The lib uses -x for the guard
        # decision (before create AND again before attach) and -xc to report the
        # count when a session is live.
        for a in "$@"; do case "$a" in -*c*)
          if [ "${INCUS_CLAUDE_RUNNING:-0}" = "1" ] || [ -n "${INCUS_CLAUDE_LIVE_FROM_CALL:-}" ]; then echo "${INCUS_CLAUDE_COUNT:-3}"; exit 0; else echo 0; exit 1; fi ;;
        esac; done
        # existence form. INCUS_CLAUDE_LIVE_FROM_CALL simulates TOCTOU: not live at
        # the first guard, live by the Nth existence check (count kept in a file).
        if [ -n "${INCUS_CLAUDE_LIVE_FROM_CALL:-}" ]; then
          _pn=$(( $(cat "${INCUS_PGREP_CALLS:-/dev/null}" 2>/dev/null || echo 0) + 1 ))
          echo "$_pn" > "${INCUS_PGREP_CALLS:-/dev/null}" 2>/dev/null || true
          [ "$_pn" -ge "$INCUS_CLAUDE_LIVE_FROM_CALL" ] && exit 0 || exit 1
        fi
        [ "${INCUS_CLAUDE_RUNNING:-0}" = "1" ] && exit 0 || exit 1 ;;
      getent) echo "ubuntu:x:1000:1000::/home/ubuntu:/bin/bash"; exit 0 ;;
      id)
        for a in "$@"; do
          [ "$a" = "-u" ] && { echo "${INCUS_UID:-1000}"; exit 0; }
          [ "$a" = "-g" ] && { echo "${INCUS_GID:-1000}"; exit 0; }
        done
        echo 1000; exit 0 ;;
      sh) exit "${INCUS_VERIFY_RC:-0}" ;;
      *) exit 0 ;;
    esac ;;
esac
exit 0
"""


def _sandbox(tmp_path, **incus_env):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    p = bindir / "incus"
    p.write_text(_INCUS_STUB)
    p.chmod(0o755)
    (tmp_path / "incus.log").touch()
    env = {
        "PATH": f"{bindir}:/usr/bin:/bin",
        "INCUS_LOG": str(tmp_path / "incus.log"),
        "INCUS_PGREP_CALLS": str(tmp_path / "pgrep_calls"),  # TOCTOU sim counter
        "HOME": str(tmp_path),  # so a stray guardian.yaml lookup can't leak
    }
    env.update({k: str(v) for k, v in incus_env.items()})
    return env


def _run(env, fn="cc_tmp_volume_apply", extra_env=None):
    if extra_env:
        env = {**env, **extra_env}
    script = f'set -euo pipefail; source "{LIB}"; {fn}; echo __DONE__'
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)


def _log(tmp_path) -> str:
    return (tmp_path / "incus.log").read_text()


# ── guards / degrade paths (must return 0, never abort the caller) ───────────


def test_lib_parses_clean():
    res = subprocess.run(["bash", "-n", str(LIB)], capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_disable_seam_skips(tmp_path):
    env = _sandbox(tmp_path, CCTMPVOL_DISABLE=1)
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout
    assert _log(tmp_path).strip() == ""  # never even called incus


def test_no_incus_skips(tmp_path):
    env = _sandbox(tmp_path)
    onlybash = tmp_path / "onlybash"
    onlybash.mkdir()
    os.symlink(shutil.which("bash"), onlybash / "bash")  # bash resolvable, incus absent
    env["PATH"] = str(onlybash)
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout


def test_container_missing_skips(tmp_path):
    env = _sandbox(tmp_path, INCUS_INFO_MISSING=1)
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout
    assert "storage volume create" not in _log(tmp_path)


def test_container_not_running_skips(tmp_path):
    env = _sandbox(tmp_path, INCUS_INFO_STATUS="Stopped")
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout
    assert "storage volume create" not in _log(tmp_path)


def test_already_attached_is_idempotent(tmp_path):
    env = _sandbox(tmp_path, INCUS_DEVICE_ATTACHED=1)
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout
    log = _log(tmp_path)
    assert "storage volume create" not in log  # no mutation on a re-run
    assert "config device add" not in log


def test_live_claude_session_skips_without_mutating(tmp_path):
    env = _sandbox(tmp_path, INCUS_CLAUDE_RUNNING=1)
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout
    log = _log(tmp_path)
    # The whole point: a live session must NOT be disturbed.
    assert "storage volume create" not in log
    assert "config device add" not in log


def test_dir_pool_skips_cosmetic_isolation(tmp_path):
    # A dir-backed pool can't enforce the size cap on its own device — applying
    # would be cosmetic (a runaway could still reach the rootfs), so the lib
    # must skip honestly rather than report a false isolation.
    env = _sandbox(tmp_path, INCUS_DRIVER="dir")
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout
    log = _log(tmp_path)
    assert "storage volume create" not in log
    assert "config device add" not in log
    assert "cosmetic" in res.stdout


# ── happy path: correct ordering + writability ──────────────────────────────


def test_happy_path_creates_attaches_and_verifies(tmp_path):
    env = _sandbox(tmp_path)  # fresh: volume absent, no claude, running
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout
    log = _log(tmp_path)
    i_create = log.index("storage volume create")
    i_add = log.index("config device add")
    i_chown = log.index("chown")
    assert i_create < i_add < i_chown  # create → attach → chown ordering
    assert "config device add genesis-cc-tmp" in log or "config device add" in log
    assert "size=2GiB" in log  # default size
    assert "config device remove" not in log  # no rollback on success
    assert "dedicated volume" in res.stdout  # success line


def test_io_limits_mirrored_one_key_each(tmp_path):
    env = _sandbox(tmp_path, INCUS_LIMIT_READ="190MB", INCUS_LIMIT_WRITE="90MB")
    res = _run(env)
    assert res.returncode == 0
    log = _log(tmp_path)
    assert "config device set genesis cc-tmp limits.read 190MB" in log
    assert "config device set genesis cc-tmp limits.write 90MB" in log


def test_empty_root_limits_sets_nothing(tmp_path):
    env = _sandbox(tmp_path, INCUS_LIMIT_READ="", INCUS_LIMIT_WRITE="")
    res = _run(env)
    assert res.returncode == 0
    assert "config device set" not in _log(tmp_path)  # nothing to mirror → no set


def test_volume_already_exists_skips_create_but_attaches(tmp_path):
    env = _sandbox(tmp_path, INCUS_VOLUME_EXISTS=0)  # show rc 0 = exists
    res = _run(env)
    assert res.returncode == 0
    log = _log(tmp_path)
    assert "storage volume create" not in log
    assert "config device add" in log  # still attaches the existing volume


# ── failure handling: unwritable volume must roll back ──────────────────────


def test_verify_failure_rolls_back_the_device(tmp_path):
    env = _sandbox(tmp_path, INCUS_VERIFY_RC=1)  # uid-1000 write fails
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout
    log = _log(tmp_path)
    assert "config device add" in log
    assert "config device remove" in log  # rolled back
    assert "not writable" in res.stdout


def test_attach_failure_keeps_volume_for_retry(tmp_path):
    env = _sandbox(tmp_path, INCUS_ATTACH_RC=1)
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout
    assert "could not attach" in res.stdout
    assert "config device remove" not in _log(tmp_path)  # nothing to roll back


# ── size + name resolution ──────────────────────────────────────────────────


def test_size_env_override(tmp_path):
    env = _sandbox(tmp_path, CCTMPVOL_SIZE_GIB=8)
    assert _run(env).returncode == 0
    assert "size=8GiB" in _log(tmp_path)


def test_bad_size_floors_to_default(tmp_path):
    env = _sandbox(tmp_path, CCTMPVOL_SIZE_GIB="abc")
    assert _run(env).returncode == 0
    assert "size=2GiB" in _log(tmp_path)


# ── remove path ─────────────────────────────────────────────────────────────


def test_remove_detaches_then_deletes(tmp_path):
    env = _sandbox(tmp_path)
    res = _run(env, fn="cc_tmp_volume_remove")
    assert res.returncode == 0 and "__DONE__" in res.stdout
    log = _log(tmp_path)
    assert log.index("config device remove") < log.index("storage volume delete")


def test_remove_skips_while_session_live(tmp_path):
    env = _sandbox(tmp_path, INCUS_CLAUDE_RUNNING=1)
    res = _run(env, fn="cc_tmp_volume_remove")
    assert res.returncode == 0
    assert "storage volume delete" not in _log(tmp_path)


# ── parse + wiring ──────────────────────────────────────────────────────────


def test_host_setup_wires_the_lib():
    """Fresh installs + retrofit: host-setup.sh must source AND call the lib."""
    text = HOST_SETUP.read_text()
    assert "lib/cc_tmp_volume.sh" in text
    assert "cc_tmp_volume_apply" in text


def test_gateway_redeploy_wires_the_lib():
    """Existing installs receive code only via the redeploy verb — it must
    invoke the lib to stderr (stdout is a parsed JSON contract)."""
    text = GATEWAY.read_text()
    assert "cc_tmp_volume.sh" in text
    idx = text.index("cc_tmp_volume_apply")
    window = text[idx : idx + 200]
    assert "1>&2" in window or ">&2" in window


def test_guardian_paths_include_the_lib_everywhere():
    """A cc_tmp_volume.sh-only change must trigger a guardian redeploy: the
    trigger list and both Python mirrors stay in lockstep."""
    assert "scripts/lib/cc_tmp_volume.sh" in UPDATE_SH.read_text()
    assert "scripts/lib/cc_tmp_volume.sh" in WATCHDOG.read_text()
    assert "scripts/lib/cc_tmp_volume.sh" in DEPLOY_HEALTH.read_text()


# ── machine-readable result tokens (consumed by the gateway cc-tmp-apply verb) ──


def _result(env, extra_env=None):
    """Run cc_tmp_volume_apply and return (rc, result_token, claude_procs)."""
    if extra_env:
        env = {**env, **extra_env}
    script = (
        f'set -euo pipefail; source "{LIB}"; '
        "cc_tmp_volume_apply >/dev/null 2>&1; "
        r'printf "RESULT=%s\nPROCS=%s\n" "${_cctmpvol_result:-}" "${_cctmpvol_claude_procs:-}"'
    )
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    res = procs = ""
    for line in r.stdout.splitlines():
        if line.startswith("RESULT="):
            res = line[len("RESULT=") :]
        elif line.startswith("PROCS="):
            procs = line[len("PROCS=") :]
    return r.returncode, res, procs


def test_result_token_disabled(tmp_path):
    rc, res, _ = _result(_sandbox(tmp_path, CCTMPVOL_DISABLE=1))
    assert rc == 0 and res == "disabled"


def test_result_token_no_incus(tmp_path):
    env = _sandbox(tmp_path)
    onlybash = tmp_path / "onlybash2"
    onlybash.mkdir()
    os.symlink(shutil.which("bash"), onlybash / "bash")
    env["PATH"] = str(onlybash)
    rc, res, _ = _result(env)
    assert rc == 0 and res == "no-incus"


def test_result_token_not_running(tmp_path):
    rc, res, _ = _result(_sandbox(tmp_path, INCUS_INFO_STATUS="Stopped"))
    assert rc == 0 and res == "not-running"


def test_result_token_already_isolated(tmp_path):
    rc, res, _ = _result(_sandbox(tmp_path, INCUS_DEVICE_ATTACHED=1))
    assert rc == 0 and res == "already-isolated"


def test_result_token_live_cc_reports_reason_and_count(tmp_path):
    # The whole point of the token: report WHY (blocked on a live session) AND how
    # many, so the awareness posture can mark the gap "converging" not "broken".
    rc, res, procs = _result(_sandbox(tmp_path, INCUS_CLAUDE_RUNNING=1, INCUS_CLAUDE_COUNT=5))
    assert rc == 0 and res == "live-cc"
    assert procs == "5"


def test_result_token_unsupported_pool(tmp_path):
    rc, res, _ = _result(_sandbox(tmp_path, INCUS_DRIVER="dir"))
    assert rc == 0 and res == "unsupported-pool"


def test_result_token_applied_on_happy_path(tmp_path):
    rc, res, procs = _result(_sandbox(tmp_path))
    assert rc == 0 and res == "applied"
    assert procs == ""  # count only populated on a live-cc skip


def test_result_token_verify_failed_rolledback(tmp_path):
    rc, res, _ = _result(_sandbox(tmp_path, INCUS_VERIFY_RC=1))
    assert rc == 0 and res == "verify-failed-rolledback"


# ── TOCTOU narrowing: re-check for a live session right before the attach ─────


def test_live_cc_between_create_and_attach_skips_attach(tmp_path):
    # Quiet at the first guard (call 1) → past create; a session launches before
    # the attach (live from call 2). The pre-attach re-check must abort the attach:
    # the volume was created (kept for reuse) but is NOT attached over the newly
    # live session's temp tree.
    env = _sandbox(tmp_path, INCUS_CLAUDE_LIVE_FROM_CALL=2)
    res = _run(env)
    assert res.returncode == 0 and "__DONE__" in res.stdout
    log = _log(tmp_path)
    assert "storage volume create" in log  # got past the first guard
    assert "config device add" not in log  # the re-check aborted the attach


def test_result_token_live_cc_at_recheck(tmp_path):
    rc, res, procs = _result(_sandbox(tmp_path, INCUS_CLAUDE_LIVE_FROM_CALL=2))
    assert rc == 0 and res == "live-cc"
    assert procs == "3"  # count reported at the re-check skip
