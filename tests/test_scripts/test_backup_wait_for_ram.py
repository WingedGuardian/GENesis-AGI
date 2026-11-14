"""backup_wait_for_ram.sh — the backup unit's ExecStartPre wait for verify memory.

The pre-step runs before backup.sh, in its own process, holding no lock and
writing nothing. It waits (up to a cap set in the UNIT's environment) until the
round-trip verify would fit in memory, so the verify does not rebuild the
database on disk and saturate a shared disk. It always exits 0: the backup runs
whatever the wait decides.

Memory is judged through the same predicate backup.sh uses
(scripts/lib/backup_verify_memory.sh). Every test except the forced-wait ones
sets the floor and margin to 0 so the result does not depend on this host's
free memory; "short" is produced by a fake cgroup with no headroom.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_WAIT = _SCRIPTS / "backup_wait_for_ram.sh"

# Independent of the host's free memory: no floor, no margin.
_MEM_OK = {
    "GENESIS_BACKUP_VERIFY_RAM_FLOOR_PCT": "0",
    "GENESIS_BACKUP_VERIFY_WAIT_MARGIN_PCT": "0",
}


@pytest.fixture
def sb(tmp_path):
    home = tmp_path / "home"
    gd = home / "genesis"
    (gd / "data").mkdir(parents=True)
    db = gd / "data" / "genesis.db"
    db.write_bytes(b"\0" * 8192)
    # A cgroup v2 tree whose root cap is 1 GiB.
    cg = tmp_path / "cgfs"
    cg.mkdir()
    (cg / "memory.max").write_text(f"{1 << 30}\n")
    (cg / "memory.stat").write_text("anon 1\ninactive_file 0\n")
    proc_cg = tmp_path / "proc-cgroup"
    proc_cg.write_text("0::/\n")
    bind = tmp_path / "bin"
    bind.mkdir()
    sleeps = tmp_path / "sleeps.log"
    return {
        "tmp": tmp_path,
        "home": home,
        "gd": gd,
        "cg": cg,
        "proc_cg": proc_cg,
        "bind": bind,
        "sleeps": sleeps,
    }


def _headroom(sb, free_bytes: int) -> None:
    """Set the fake root cgroup so it has ``free_bytes`` of headroom."""
    (sb["cg"] / "memory.current").write_text(f"{(1 << 30) - free_bytes}\n")


def _shim_sleep(sb, free_after: int | None = None) -> None:
    """A ``sleep`` that records its argument and does not sleep. With
    ``free_after``, the first call also frees that much cgroup headroom."""
    body = f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "{sb["sleeps"]}"\n'
    if free_after is not None:
        current = (1 << 30) - free_after
        body += f'printf "%s\\n" "{current}" > "{sb["cg"] / "memory.current"}"\n'
    body += "exit 0\n"
    path = sb["bind"] / "sleep"
    path.write_text(body)
    path.chmod(0o755)


def _run(sb, *, secrets: str | None = None, script: Path = _WAIT, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GENESIS_BACKUP_")}
    env.pop("SECRETS_PATH", None)
    env.update(
        HOME=str(sb["home"]),
        GENESIS_DIR=str(sb["gd"]),
        GENESIS_BACKUP_CGROUP_FS=str(sb["cg"]),
        GENESIS_BACKUP_PROC_CGROUP=str(sb["proc_cg"]),
        PATH=f"{sb['bind']}:{os.environ['PATH']}",
    )
    if secrets is not None:
        (sb["gd"] / "secrets.env").write_text(secrets)
    env.update(extra)
    proc = subprocess.run(
        ["bash", str(script)],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    sleeps = sb["sleeps"].read_text().split() if sb["sleeps"].exists() else []
    return proc, sleeps


def test_fits_at_once_does_not_sleep(sb):
    _headroom(sb, 512 << 20)
    _shim_sleep(sb)
    proc, sleeps = _run(sb, GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3600", **_MEM_OK)
    assert proc.returncode == 0, proc.stderr
    assert sleeps == []
    assert "verify memory fits" in proc.stdout, proc.stdout


def test_short_throughout_waits_to_the_cap_then_proceeds(sb):
    _headroom(sb, 0)
    _shim_sleep(sb)
    proc, sleeps = _run(
        sb,
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3",
        GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S="1",
        **_MEM_OK,
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps, "a short run must sleep"
    assert all(s == "1" for s in sleeps), sleeps
    assert "waiting for verify memory" in proc.stdout, proc.stdout
    assert "still short" in proc.stdout, proc.stdout


def test_memory_frees_mid_wait(sb):
    _headroom(sb, 0)
    _shim_sleep(sb, free_after=512 << 20)
    proc, sleeps = _run(
        sb,
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3600",
        GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S="600",
        **_MEM_OK,
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps == ["600"], sleeps
    assert "verify memory fits" in proc.stdout, proc.stdout


@pytest.mark.parametrize("cap", [None, "0", "abc", "-5"])
def test_no_cap_means_no_wait(sb, cap):
    _headroom(sb, 0)
    _shim_sleep(sb)
    extra = dict(_MEM_OK)
    if cap is not None:
        extra["GENESIS_BACKUP_VERIFY_WAIT_MAX_S"] = cap
    proc, sleeps = _run(sb, **extra)
    assert proc.returncode == 0, proc.stderr
    assert sleeps == []
    assert "waiting" not in proc.stdout, proc.stdout


@pytest.mark.parametrize(("interval", "want"), [("abc", "600"), ("0", "600"), ("08", "8")])
def test_interval_falls_back_and_never_spins(sb, interval, want):
    _headroom(sb, 0)
    _shim_sleep(sb, free_after=512 << 20)
    proc, sleeps = _run(
        sb,
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3600",
        GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S=interval,
        **_MEM_OK,
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps == [want], sleeps


def test_sleep_is_clamped_to_the_time_left(sb):
    _headroom(sb, 0)
    _shim_sleep(sb)
    proc, sleeps = _run(
        sb,
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="2",
        GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S="600",
        **_MEM_OK,
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps and all(int(s) <= 2 for s in sleeps), sleeps


def test_verify_memory_off_means_no_wait(sb):
    _headroom(sb, 0)
    _shim_sleep(sb)
    proc, sleeps = _run(
        sb,
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3600",
        GENESIS_BACKUP_VERIFY_MEMORY="off",
        **_MEM_OK,
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps == []


def test_verify_memory_off_in_secrets_file_means_no_wait(sb):
    """The dashboard writes the backup knobs to secrets.env, so the pre-step
    must read them the way backup.sh does."""
    _headroom(sb, 0)
    _shim_sleep(sb)
    proc, sleeps = _run(
        sb,
        secrets="GENESIS_BACKUP_VERIFY_MEMORY=off\n",
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3600",
        **_MEM_OK,
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps == []


def test_floor_from_secrets_file_is_honoured(sb):
    """A floor of 99% set only in secrets.env leaves no room: it must wait."""
    _headroom(sb, 512 << 20)
    _shim_sleep(sb)
    proc, sleeps = _run(
        sb,
        secrets="GENESIS_BACKUP_VERIFY_RAM_FLOOR_PCT=99\n",
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="2",
        GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S="1",
        GENESIS_BACKUP_VERIFY_WAIT_MARGIN_PCT="0",
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps, proc.stdout
    assert "waiting for verify memory" in proc.stdout, proc.stdout


def test_cap_in_secrets_file_is_ignored(sb):
    """The cap belongs to the unit, beside TimeoutStartSec: a secrets.env value
    must not start a wait the unit's timeout was not sized for."""
    _headroom(sb, 0)
    _shim_sleep(sb)
    proc, sleeps = _run(sb, secrets="GENESIS_BACKUP_VERIFY_WAIT_MAX_S=999999\n", **_MEM_OK)
    assert proc.returncode == 0, proc.stderr
    assert sleeps == []


def test_unit_cap_wins_over_a_secrets_file_cap(sb):
    """Both set: the unit's 2 s bounds the first sleep (a 600 s interval is
    clamped to it). Had secrets.env's 999999 won, the first sleep would be 600."""
    _headroom(sb, 0)
    _shim_sleep(sb, free_after=512 << 20)
    proc, sleeps = _run(
        sb,
        secrets="GENESIS_BACKUP_VERIFY_WAIT_MAX_S=999999\n",
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="2",
        GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S="600",
        **_MEM_OK,
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps == ["2"], sleeps


@pytest.mark.parametrize(("secrets_cap", "want"), [("0", []), ("2", ["2"]), ("junk", ["600"])])
def test_secrets_file_may_lower_the_unit_cap(sb, secrets_cap, want):
    """secrets.env can lower the unit's 3600 s (0 turns the wait off) but an
    invalid value changes nothing: the 600 s interval then fits the cap."""
    _headroom(sb, 0)
    _shim_sleep(sb, free_after=512 << 20)
    proc, sleeps = _run(
        sb,
        secrets=f"GENESIS_BACKUP_VERIFY_WAIT_MAX_S={secrets_cap}\n",
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3600",
        GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S="600",
        **_MEM_OK,
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps == want, sleeps


def test_a_line_above_all_memory_never_waits(sb):
    """need + floor + margin above MemTotal can never be met: no hour-long
    wait that ends on disk anyway."""
    _headroom(sb, 512 << 20)
    _shim_sleep(sb)
    proc, sleeps = _run(
        sb,
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3600",
        GENESIS_BACKUP_VERIFY_RAM_FLOOR_PCT="100",
        GENESIS_BACKUP_VERIFY_WAIT_MARGIN_PCT="5",
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps == []
    assert "can never fit" in proc.stdout, proc.stdout


def test_logs_say_whether_backup_sh_line_is_met(sb):
    """Inside the margin band: backup.sh's own line is met though the wait's is
    not, and every wait line says so."""
    _headroom(sb, 64 << 20)
    _shim_sleep(sb)
    proc, _ = _run(
        sb,
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="1",
        GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S="1",
        GENESIS_BACKUP_VERIFY_RAM_FLOOR_PCT="0",
        GENESIS_BACKUP_VERIFY_WAIT_MARGIN_PCT="1",
    )
    assert proc.returncode == 0, proc.stderr
    assert "backup.sh's line met: yes" in proc.stdout, proc.stdout


def test_margin_keeps_waiting_when_only_the_line_is_met(sb):
    """Fits need + floor but not the margin: the pre-step keeps waiting,
    because backup.sh decides minutes later and memory moves."""
    _headroom(sb, 64 << 20)
    _shim_sleep(sb)
    total_kb = int(
        next(
            ln
            for ln in Path("/proc/meminfo").read_text().splitlines()
            if ln.startswith("MemTotal:")
        ).split()[1]
    )
    # 1% of MemTotal exceeds 64 MiB of headroom on any host with >6.4 GiB.
    if total_kb // 100 <= 64 << 10:
        pytest.skip("host MemTotal too small for a 1% margin to exceed 64 MiB")
    proc, sleeps = _run(
        sb,
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="2",
        GENESIS_BACKUP_VERIFY_WAIT_INTERVAL_S="1",
        GENESIS_BACKUP_VERIFY_RAM_FLOOR_PCT="0",
        GENESIS_BACKUP_VERIFY_WAIT_MARGIN_PCT="1",
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps, proc.stdout


def test_fence_checks_a_genesis_dir_secrets_file_repoints(sb):
    """secrets.env may re-point GENESIS_DIR after the first fence check. The
    fence must run on the value actually used: a fenced tree is never read."""
    fenced = sb["tmp"] / "fenced-genesis"
    (fenced / "data").mkdir(parents=True)
    (fenced / "data" / "genesis.db").write_bytes(b"\0" * 8192)
    fence_log = sb["tmp"] / "fence.log"
    _headroom(sb, 0)
    _shim_sleep(sb)
    proc, sleeps = _run(
        sb,
        secrets=f"GENESIS_DIR={fenced}\n",
        GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3600",
        GENESIS_TEST_FENCE_LIVE_ROOTS=str(fenced),
        GENESIS_TEST_FENCE_LOG=str(fence_log),
        **_MEM_OK,
    )
    assert proc.returncode == 0, proc.stderr
    assert sleeps == []
    assert "REFUSED (test fence)" in fence_log.read_text()


def test_missing_database_does_not_wait(sb):
    (sb["gd"] / "data" / "genesis.db").unlink()
    _headroom(sb, 0)
    _shim_sleep(sb)
    proc, sleeps = _run(sb, GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3600", **_MEM_OK)
    assert proc.returncode == 0, proc.stderr
    assert sleeps == []


def test_a_missing_lib_still_exits_zero_and_says_so(sb):
    """A partial deploy (the script without its lib) must not stop the backup:
    exit 0 and a loud line, never a silent skip."""
    lone = sb["tmp"] / "lone" / "scripts"
    lone.mkdir(parents=True)
    copy = lone / _WAIT.name
    copy.write_text(_WAIT.read_text())
    _headroom(sb, 0)
    _shim_sleep(sb)
    proc, sleeps = _run(sb, script=copy, GENESIS_BACKUP_VERIFY_WAIT_MAX_S="3600", **_MEM_OK)
    assert proc.returncode == 0, proc.stderr
    assert sleeps == []
    assert "continuing without the wait" in proc.stdout + proc.stderr
