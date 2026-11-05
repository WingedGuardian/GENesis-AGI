"""lobby-door.sh starts the tmux server in a scope of its own when none runs.

The door's first tmux create can be the call that STARTS the server, and every
slot's terminal belongs to that server. Fake tmux and a recording slot_exec.sh
stand in for the real ones, so nothing here needs systemd or a tmux server.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_DOOR = Path(__file__).resolve().parents[2] / "scripts" / "lobby-door.sh"

_FAKE_TMUX = """#!/usr/bin/env bash
echo "tmux $*" >> "$CALLS"
case "$*" in
    *list-sessions*) [ -n "${SERVER_UP:-}" ] && { echo lobby; exit 0; }; exit 1 ;;
esac
exit 0
"""

_FAKE_SLOT_EXEC = """#!/usr/bin/env bash
echo "slot_exec $*" >> "$CALLS"
while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""


@pytest.fixture()
def door(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    shutil.copy2(_DOOR, scripts / "lobby-door.sh")
    (scripts / "slot_exec.sh").write_text(_FAKE_SLOT_EXEC)
    (scripts / "slot_exec.sh").chmod(0o755)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "tmux").write_text(_FAKE_TMUX)
    (bindir / "tmux").chmod(0o755)
    calls = tmp_path / "calls.log"

    def run(server_up: bool) -> list[str]:
        env = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "CALLS": str(calls)}
        if server_up:
            env["SERVER_UP"] = "1"
        subprocess.run(["bash", str(scripts / "lobby-door.sh")], env=env,
                       capture_output=True, text=True, timeout=30, check=False)
        return calls.read_text().splitlines()

    return run


def test_no_server_starts_it_in_an_omit_scope(door):
    calls = door(server_up=False)
    wrapped = [c for c in calls if c.startswith("slot_exec ")]
    assert wrapped == ["slot_exec --unit-prefix genesis-tmux --slice app.slice --omit server -- tmux new-session -d -s lobby"], calls
    assert any(c.startswith("tmux -u new-session -s lobby-") for c in calls), calls


def test_running_server_is_joined_unwrapped(door):
    calls = door(server_up=True)
    assert not [c for c in calls if c.startswith("slot_exec ")], calls
