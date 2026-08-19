"""cc_exit_capture.sh — durable record of why a CC session died.

A CC session runs as tmux ``... exec claude`` (cc-slot.sh). When claude exits the
pane vanishes with its output, so a dying session leaves no trace — the
2026-08-19 death was undiagnosable for exactly this reason. cc-slot.sh now drops
the inner ``exec`` and calls cc_exit_capture.sh on claude's return to log the
exit status (signal-decoded) + a pane-scrollback tail.

Two layers: (1) the script's status/hint/rotation logic, tested directly with
TMUX_PANE unset (no tmux needed); (2) the FULL flow — the exact cc-slot inner
command shape with a fake crashing ``claude`` — in a scratch tmux server on a
private ``-L`` socket, so the real cc-* sessions are never touched.
"""

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

_CAPTURE = Path(__file__).resolve().parents[2] / "scripts" / "cc_exit_capture.sh"


def _run_capture(home: Path, slot: str, ec: str, tmux_pane: str | None = None):
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("TMUX_PANE", None)
    if tmux_pane is not None:
        env["TMUX_PANE"] = tmux_pane
    return subprocess.run(
        [str(_CAPTURE), slot, ec],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def _log_for(home: Path, slot: str) -> Path:
    return home / ".genesis" / "logs" / f"cc_exit_{slot}.log"


# ── Layer 1: script logic, no tmux ───────────────────────────────────────


def test_records_exit_status_and_clean_hint(tmp_path):
    home = tmp_path / "home"
    r = _run_capture(home, "1", "0")
    assert r.returncode == 0, r.stderr
    body = _log_for(home, "1").read_text()
    assert "cc-1 claude exited status=0" in body
    assert "clean exit" in body


def test_decodes_fatal_signals(tmp_path):
    home = tmp_path / "home"
    cases = {
        "134": "SIGABRT",  # V8/Node fatal abort
        "137": "SIGKILL",  # OOM kill
        "139": "SIGSEGV",
        "143": "SIGTERM",
    }
    for ec in cases:
        _run_capture(home, "2", ec)
    body = _log_for(home, "2").read_text()
    for ec, marker in cases.items():
        assert f"status={ec}" in body, body
        assert marker in body, f"missing {marker} decode:\n{body}"


def test_never_fails_when_home_unwritable(tmp_path):
    """Best-effort: a capture problem must never surface a non-zero exit (it would
    otherwise change the session's exit code). Point HOME at a non-dir."""
    bad = tmp_path / "afile"
    bad.write_text("not a dir")
    env = dict(os.environ)
    env["HOME"] = str(bad)
    env.pop("TMUX_PANE", None)
    r = subprocess.run(
        [str(_CAPTURE), "1", "137"],
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert r.returncode == 0, f"capture must exit 0 even when it cannot log: {r.stderr}"


def test_log_self_rotates(tmp_path):
    home = tmp_path / "home"
    log = _log_for(home, "3")
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(f"old line {i}" for i in range(5000)) + "\n")
    _run_capture(home, "3", "0")
    lines = log.read_text().splitlines()
    assert len(lines) <= 2000 + 5, f"log not rotated: {len(lines)} lines"
    assert "cc-3 claude exited status=0" in log.read_text()


# ── Layer 2: full flow in a scratch tmux (never the live sessions) ───────


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux not available")
def test_full_flow_captures_crash_in_scratch_tmux(tmp_path):
    """Mimic cc-slot's exact inner command with a fake crashing claude, in a
    private tmux server. Assert the exit status AND the pane's dying words are
    captured, and the session actually ends (exit-code preservation)."""
    home = tmp_path / "home"
    (home / ".genesis" / "logs").mkdir(parents=True)
    marker = f"CRASH_MARKER_{uuid.uuid4().hex[:8]}"
    fake_claude = tmp_path / "fakeclaude.sh"
    fake_claude.write_text(f"#!/usr/bin/env bash\necho '{marker}'\nexit 134\n")
    fake_claude.chmod(0o755)

    sock = f"ccexit-test-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    # The exact shape cc-slot.sh builds: run claude (no exec), capture on return,
    # preserve the exit code. HOME is set so capture logs into our sandbox.
    inner = (
        f"HOME={home} '{fake_claude}'; __ec=$?; "
        f"HOME={home} '{_CAPTURE}' 7 $__ec >/dev/null 2>&1; exit $__ec"
    )
    try:
        subprocess.run(
            ["tmux", "-L", sock, "new-session", "-d", "-s", "t", inner],
            check=True,
            capture_output=True,
            text=True,
        )
        # Condition-based wait (bounded): the session ends when the inner cmd exits.
        deadline = time.monotonic() + 15
        gone = False
        while time.monotonic() < deadline:
            r = subprocess.run(["tmux", "-L", sock, "has-session", "-t", "t"], capture_output=True)
            if r.returncode != 0:
                gone = True
                break
            time.sleep(0.2)
        assert gone, "session never ended — exit-code preservation broken"
    finally:
        subprocess.run(["tmux", "-L", sock, "kill-server"], capture_output=True)

    body = _log_for(home, "7").read_text()
    assert "cc-7 claude exited status=134" in body, body
    assert "SIGABRT" in body, body
    assert marker in body, f"pane dying words not captured:\n{body}"
