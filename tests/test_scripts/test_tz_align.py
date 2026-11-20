"""Behavioral tests for scripts/tz_align.sh.

The harness copies the REAL script into a tmp GENESIS_ROOT that links this
checkout's ``src`` and ``config`` and the running venv python, and puts stub
``ssh``, ``timedatectl`` and ``sudo`` on PATH, each logging its calls. HOME is
the same tmp root, so genesis.yaml and the guardian files are the test's own.

Contract: the lever and a chosen zone gate everything; with a guardian, one
``set-timezone <zone>`` gateway call whose JSON decides the exit (0 for done,
unreachable or an old gateway; 3 for a clock that could not be set); without
one, the container clock through ``sudo -n timedatectl``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "tz_align.sh"

# A dashboard change during the call: each file in $TZA_NEXT (sorted) replaces
# genesis.yaml, one per call, as a zone picked while the run was applying.
_STUB_SSH = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TZA_LOG.ssh"
if [ -n "${TZA_NEXT:-}" ]; then
    next="$(ls "$TZA_NEXT" 2>/dev/null | head -n 1)"
    [ -n "$next" ] && mv "$TZA_NEXT/$next" "$HOME/.genesis/config/genesis.yaml"
fi
cat "$TZA_SSH_OUT"
[ -f "$TZA_SSH_ERR" ] && cat "$TZA_SSH_ERR" >&2
exit "${TZA_SSH_RC:-0}"
"""

_STUB_TIMEDATECTL = r"""#!/usr/bin/env bash
printf 'timedatectl %s\n' "$*" >> "$TZA_LOG"
case "$1" in
    show) cat "$TZA_CLOCK" ;;
    set-timezone) [ -n "${TZA_SET_IGNORED:-}" ] || printf '%s' "$2" > "$TZA_CLOCK" ;;
esac
"""

_STUB_SUDO = r"""#!/usr/bin/env bash
printf 'sudo %s\n' "$*" >> "$TZA_LOG"
[ "$1" = -n ] && shift
[ -n "${TZA_NO_SUDO:-}" ] && exit 1
[ "$1" = true ] && exit 0
exec "$@"
"""


class Harness:
    def __init__(self, tmp_path: Path, *, guardian: bool = True, key: bool = True):
        self.root = tmp_path
        (tmp_path / "scripts" / "lib").mkdir(parents=True)
        (tmp_path / "scripts" / "tz_align.sh").write_text(SCRIPT.read_text())
        (tmp_path / "scripts" / "lib" / "timezone.sh").write_text(
            (REPO_ROOT / "scripts" / "lib" / "timezone.sh").read_text()
        )
        (tmp_path / "src").symlink_to(REPO_ROOT / "src")
        (tmp_path / "config").symlink_to(REPO_ROOT / "config")
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        (tmp_path / ".venv" / "bin" / "python").symlink_to(sys.executable)
        self.cfg = tmp_path / ".genesis" / "config"
        self.cfg.mkdir(parents=True)
        if guardian:
            (tmp_path / ".genesis" / "guardian_remote.yaml").write_text(
                "host_ip: 192.0.2.9\nhost_user: opuser\n"
            )
        if key:
            (tmp_path / ".ssh").mkdir()
            (tmp_path / ".ssh" / "genesis_guardian_ed25519").write_text("KEY")
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, body in (
            ("ssh", _STUB_SSH),
            ("timedatectl", _STUB_TIMEDATECTL),
            ("sudo", _STUB_SUDO),
        ):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log = tmp_path / "calls.log"
        self.log.touch()
        self.clock = tmp_path / "clock"
        self.clock.write_text("UTC")
        self.ssh_out = tmp_path / "ssh.out"
        self.ssh_out.write_text("")
        self.ssh_err = tmp_path / "ssh.err"

    def zone(self, value, **extra) -> None:
        (self.cfg / "genesis.yaml").write_text(json.dumps({"timezone": value, **extra}))

    def gateway(self, stdout: str = "", stderr: str = "", rc: int = 0) -> None:
        self.ssh_out.write_text(stdout)
        if stderr:
            self.ssh_err.write_text(stderr)
        self.rc = rc

    def run(self, **extra_env: str) -> subprocess.CompletedProcess[str]:
        env = {
            "HOME": str(self.root),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "TZA_LOG": str(self.log),
            "TZA_CLOCK": str(self.clock),
            "TZA_SSH_OUT": str(self.ssh_out),
            "TZA_SSH_ERR": str(self.ssh_err),
            "TZA_SSH_RC": str(getattr(self, "rc", 0)),
            **extra_env,
        }
        return subprocess.run(
            ["bash", str(self.root / "scripts" / "tz_align.sh")],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def ssh_calls(self) -> list[str]:
        p = Path(f"{self.log}.ssh")
        return p.read_text().splitlines() if p.exists() else []

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()


def _ok(container: str = "set", changed: bool = True) -> str:
    return json.dumps(
        {
            "ok": True,
            "action": "set-timezone",
            "zone": "Europe/Paris",
            "from": "UTC",
            "changed": changed,
            "container": container,
        }
    )


def _queue_zones(h: Harness, *zones: str) -> dict[str, str]:
    """Zones the 'dashboard' picks during successive gateway calls."""
    nxt = h.root / "next"
    nxt.mkdir()
    for i, z in enumerate(zones):
        (nxt / f"{i:02d}.yaml").write_text(json.dumps({"timezone": z}))
    return {"TZA_NEXT": str(nxt)}


def test_parses_clean():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_sets_both_clocks_through_the_gateway(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway(_ok())
    proc = h.run()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "clocks at Europe/Paris" in proc.stdout
    assert len(h.ssh_calls()) == 1
    assert h.ssh_calls()[0].endswith("opuser@192.0.2.9 set-timezone Europe/Paris")


@pytest.mark.parametrize("container", ["unchanged", "no-incus"])
def test_other_container_outcomes_are_success(tmp_path, container):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway(_ok(container=container, changed=False))
    assert h.run().returncode == 0


@pytest.mark.parametrize(
    "stdout,stderr",
    [
        (_ok(container="failed"), ""),
        (
            "",
            '{"ok": false, "action": "set-timezone", "error": "host timedatectl set-timezone failed"}',
        ),
        ("", '{"ok": false, "action": "set-timezone", "error": "passwordless sudo unavailable"}'),
    ],
)
def test_a_clock_that_could_not_be_set_fails_the_unit(tmp_path, stdout, stderr):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway(stdout, stderr, rc=0 if stdout else 1)
    proc = h.run()
    assert proc.returncode == 3, proc.stdout
    assert "could not set the clocks" in proc.stdout


def test_an_old_gateway_is_skipped(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway("", '{"ok": false, "error": "denied"}', rc=1)
    proc = h.run()
    assert proc.returncode == 0 and "predates set-timezone" in proc.stdout


@pytest.mark.parametrize("stderr", ["", "ssh: connect to host 192.0.2.9 port 22: No route to host"])
def test_an_unreachable_host_is_skipped(tmp_path, stderr):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway("", stderr, rc=255)
    proc = h.run()
    assert proc.returncode == 0 and "unreachable" in proc.stdout


@pytest.mark.parametrize(
    "cfg",
    [
        None,  # no genesis.yaml
        {"timezone": ""},
        {"timezone": "UTC"},  # the shipped placeholder
        {"timezone": "Etc/UTC"},
        {"timezone": "Mars/Base"},
        {"timezone": 5},
    ],
)
def test_no_chosen_zone_moves_nothing(tmp_path, cfg):
    h = Harness(tmp_path)
    if cfg is not None:
        (h.cfg / "genesis.yaml").write_text(json.dumps(cfg))
    proc = h.run()
    assert proc.returncode == 0 and "no zone chosen" in proc.stdout
    assert h.ssh_calls() == [] and h.calls() == []


def test_a_utc_the_dashboard_chose_is_applied(tmp_path):
    h = Harness(tmp_path)
    h.zone("UTC", timezone_chosen=True)
    h.gateway(_ok(changed=False))
    assert h.run().returncode == 0
    assert h.ssh_calls()[0].endswith("set-timezone UTC")


def test_the_kill_switch_moves_nothing(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    proc = h.run(GENESIS_TZ_ALIGN_DISABLED="1")
    assert proc.returncode == 0 and "mode is off" in proc.stdout
    assert h.ssh_calls() == []


def test_a_missing_guardian_key_is_skipped(tmp_path):
    h = Harness(tmp_path, key=False)
    h.zone("Europe/Paris")
    proc = h.run()
    assert proc.returncode == 0 and "key" in proc.stdout and h.ssh_calls() == []


def test_without_a_guardian_the_container_clock_is_set(tmp_path):
    h = Harness(tmp_path, guardian=False)
    h.zone("Europe/Paris")
    proc = h.run()
    assert proc.returncode == 0, proc.stdout
    assert h.clock.read_text() == "Europe/Paris"
    assert "sudo -n timedatectl set-timezone Europe/Paris" in h.calls()
    assert h.ssh_calls() == []


def test_without_a_guardian_an_aligned_clock_is_left_alone(tmp_path):
    h = Harness(tmp_path, guardian=False)
    h.zone("Europe/Paris")
    h.clock.write_text("Europe/Paris")
    proc = h.run()
    assert proc.returncode == 0 and "already" in proc.stdout
    assert not any(c.startswith("sudo") for c in h.calls())


def test_without_a_guardian_or_sudo_nothing_moves(tmp_path):
    h = Harness(tmp_path, guardian=False)
    h.zone("Europe/Paris")
    proc = h.run(TZA_NO_SUDO="1")
    assert proc.returncode == 0 and "no passwordless sudo" in proc.stdout
    assert h.clock.read_text() == "UTC"


def test_without_a_guardian_a_read_back_mismatch_fails(tmp_path):
    h = Harness(tmp_path, guardian=False)
    h.zone("Europe/Paris")
    assert h.run(TZA_SET_IGNORED="1").returncode == 3


def test_a_second_run_is_single_flight(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    lock = tmp_path / ".genesis" / "locks" / "tz_align.lock"
    lock.parent.mkdir(parents=True)
    # Blocking, so the readiness probe below can never make the holder give up.
    holder = subprocess.Popen(["flock", str(lock), "sleep", "30"])
    try:
        for _ in range(100):  # wait until the holder has the lock
            if subprocess.run(["flock", "-n", str(lock), "true"]).returncode != 0:
                break
            subprocess.run(["sleep", "0.05"])
        proc = h.run()
    finally:
        holder.kill()
        holder.wait()
    assert proc.returncode == 0 and "another run" in proc.stdout
    assert h.ssh_calls() == []


def test_the_units_render_the_script_and_the_midday_schedule():
    svc = (REPO_ROOT / "scripts" / "systemd" / "genesis-tz-align.service.template").read_text()
    timer = (REPO_ROOT / "scripts" / "systemd" / "genesis-tz-align.timer.template").read_text()
    assert "ExecStart=/bin/bash __REPO_DIR__/scripts/tz_align.sh" in svc
    assert "ProtectSystem=strict" in svc and "ReadWritePaths=%h" in svc
    # The guardian-less leg needs sudo, which NoNewPrivileges would break.
    assert not any(ln.startswith("NoNewPrivileges=") for ln in svc.splitlines())
    assert "OnCalendar=*-*-* 12:20:00" in timer and "Persistent=true" in timer
    assert os.access(SCRIPT, os.R_OK)


def test_a_hung_host_is_a_failure_not_unreachable(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway("", "", rc=124)  # timeout's exit code: connected, then no answer
    proc = h.run()
    assert proc.returncode == 3 and "did not answer" in proc.stdout


def test_the_last_json_line_decides(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway(_ok(container="failed") + "\n" + _ok())
    assert h.run().returncode == 0
    h2 = Harness(tmp_path / "second")
    h2.zone("Europe/Paris")
    h2.gateway(_ok() + "\n" + _ok(container="failed"))
    assert h2.run().returncode == 3


def test_a_reply_without_a_container_outcome_is_a_failure(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway(json.dumps({"ok": True, "action": "set-timezone", "zone": "Europe/Paris"}))
    assert h.run().returncode == 3


def test_a_zone_picked_during_the_run_is_applied_too(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway(_ok())
    proc = h.run(**_queue_zones(h, "America/Argentina/Buenos_Aires"))
    assert proc.returncode == 0, proc.stdout
    calls = h.ssh_calls()
    assert [c.rsplit(" ", 1)[-1] for c in calls] == [
        "Europe/Paris",
        "America/Argentina/Buenos_Aires",
    ]
    assert "changed to America/Argentina/Buenos_Aires during this run" in proc.stdout


def test_a_zone_that_keeps_changing_stops_after_three_passes(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway(_ok())
    proc = h.run(**_queue_zones(h, "Asia/Tokyo", "Europe/Paris", "Asia/Tokyo", "Europe/Paris"))
    assert proc.returncode == 0
    assert len(h.ssh_calls()) == 3
    assert "keeps changing" in proc.stdout


def test_a_hostile_deeply_nested_reply_is_only_unparseable(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway("{" + '"a":[' * 5000 + "]" * 5000 + "}")
    proc = h.run()
    assert proc.returncode == 0 and "Traceback" not in proc.stdout + proc.stderr


def _stamp(h: Harness, age_s: int, zone: str) -> Path:
    state = h.root / ".genesis" / "state"
    state.mkdir(parents=True, exist_ok=True)
    p = state / "tz_align_last_move"
    p.write_text(f"{int(time.time()) - age_s} {zone}\n")
    return p


def test_a_move_to_another_zone_waits_for_the_cooldown(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    _stamp(h, 600, "Asia/Tokyo")  # moved 10 min ago
    proc = h.run()
    assert proc.returncode == 0 and "60-min cooldown" in proc.stdout
    assert h.ssh_calls() == []


def test_the_same_zone_inside_the_cooldown_still_runs(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    _stamp(h, 600, "Europe/Paris")
    h.gateway(_ok(changed=False))
    assert h.run().returncode == 0 and len(h.ssh_calls()) == 1


def test_an_old_move_does_not_hold_the_next_one(tmp_path):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    _stamp(h, 3700, "Asia/Tokyo")
    h.gateway(_ok())
    assert h.run().returncode == 0 and len(h.ssh_calls()) == 1


@pytest.mark.parametrize("changed,stamped", [(True, True), (False, False)])
def test_only_a_real_host_move_is_stamped(tmp_path, changed, stamped):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    h.gateway(_ok(changed=changed))
    assert h.run().returncode == 0
    p = h.root / ".genesis" / "state" / "tz_align_last_move"
    assert p.exists() is stamped
    if stamped:
        at, zone = p.read_text().split()
        assert zone == "Europe/Paris" and abs(int(at) - time.time()) < 60


def test_the_cooldown_is_for_the_host_only(tmp_path):
    h = Harness(tmp_path, guardian=False)
    h.zone("Europe/Paris")
    _stamp(h, 60, "Asia/Tokyo")
    assert h.run().returncode == 0
    assert h.clock.read_text() == "Europe/Paris"


@pytest.mark.parametrize("stamp", ["future", "99999999999999999999 Asia/Tokyo", "garbage", ""])
def test_a_bad_stamp_never_holds_a_move(tmp_path, stamp):
    h = Harness(tmp_path)
    h.zone("Europe/Paris")
    p = _stamp(h, 0, "Asia/Tokyo")
    if stamp == "future":
        p.write_text(f"{int(time.time()) + 86400} Asia/Tokyo\n")
    else:
        p.write_text(stamp)
    h.gateway(_ok())
    assert h.run().returncode == 0 and len(h.ssh_calls()) == 1
