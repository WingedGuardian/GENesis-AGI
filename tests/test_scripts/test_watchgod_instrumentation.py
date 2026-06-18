"""tmp_watchgod instrumentation — pressure logging + top-consumers snapshot.

Before this, the watchgod logged only the tier name, so a filled-cc-tmp incident
couldn't be diagnosed once the nuclear cleanup erased the evidence. These verify
`_log_cc_pressure` writes a persisted top-consumers snapshot (under the log dir, which
survives the cleanup) + a measured log line, and that `check_cc_tmp` invokes it at RED
BEFORE cleaning up.

tmux is STUBBED to a no-op so the cleanup's kill-idle-sessions loop can never touch a
real CC session during the test (it would otherwise reap unattached cc-* tmux sessions).
The script's new sourcing guard lets us load its functions without starting the daemon.
"""

import os
import stat
import subprocess
from pathlib import Path

_WATCHGOD = Path(__file__).resolve().parents[2] / "scripts" / "tmp_watchgod.sh"


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _sandbox(tmp_path):
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    cctmp = home / ".genesis" / "cc-tmp"   # the script's default CC_TMP_DIR under HOME
    cctmp.mkdir(parents=True)
    bind = tmp_path / "bin"
    bind.mkdir()
    # Bulletproof: tmux returns NOTHING for list-sessions, so the kill loop has no input
    # and never reaps a real session; any tmux call is a harmless exit-0 no-op.
    _make_stub(bind / "tmux", "#!/usr/bin/env bash\nexit 0\n")
    return home, cctmp, bind


def _run(home, bind, snippet):
    env = dict(os.environ)
    env.update(HOME=str(home), PATH=f"{bind}:{os.environ['PATH']}")
    return subprocess.run(
        ["bash", "-c", f"source '{_WATCHGOD}'\n{snippet}"],
        env=env, capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )


def test_log_cc_pressure_writes_snapshot_and_logline(tmp_path):
    home, cctmp, bind = _sandbox(tmp_path)
    (cctmp / "big.bin").write_bytes(b"x" * (3 * 1024 * 1024))  # a 3MB consumer
    proc = _run(home, bind, "_log_cc_pressure red 460 120")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    logs = home / ".genesis" / "logs"
    snaps = list(logs.glob("cc_tmp_top_*.txt"))
    assert snaps, "no persisted top-consumers snapshot written"
    body = snaps[0].read_text()
    assert "tier=red used=460MB free=120MB" in body, body
    assert "big.bin" in body, f"top consumers not captured:\n{body}"
    logtext = (logs / "tmp_watchgod.log").read_text()
    assert "cc-tmp RED: used=460MB free=120MB" in logtext, logtext


def test_check_cc_tmp_red_logs_pressure_before_cleanup(tmp_path):
    """check_cc_tmp at RED records pressure BEFORE the nuclear cleanup erases it."""
    home, cctmp, bind = _sandbox(tmp_path)
    (cctmp / "big.bin").write_bytes(b"x" * (5 * 1024 * 1024))  # 5MB
    # budget=4 → RED threshold 3MB; 5MB used → RED. sacred=1 so disk-free never trips it.
    out = _run(home, bind, "CC_TMP_BUDGET_MB=4; SACRED_GROUND_MB=1; check_cc_tmp")
    assert out.returncode == 0, f"{out.stdout}\n{out.stderr}"
    assert out.stdout.strip().startswith("red:"), out.stdout
    snaps = list((home / ".genesis" / "logs").glob("cc_tmp_top_*.txt"))
    assert snaps, "RED did not write a pressure snapshot before cleanup"


def test_pressure_snapshots_are_bounded(tmp_path):
    """A sustained ORANGE/RED episode must not accumulate snapshots unbounded — the
    helper keeps only the most recent ~20 so it can't fill the fs it's protecting."""
    home, cctmp, bind = _sandbox(tmp_path)
    logs = home / ".genesis" / "logs"
    for i in range(25):  # pre-seed 25 stale snapshots
        (logs / f"cc_tmp_top_20260101T0000{i:02d}Z.txt").write_text("old")
    proc = _run(home, bind, "_log_cc_pressure orange 400 200")
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
    remaining = list(logs.glob("cc_tmp_top_*.txt"))
    assert len(remaining) <= 20, f"snapshots not bounded: {len(remaining)} remain"
