"""Guardrail: the lobby door must not make tmux emit a status-line message.

THE BUG THIS PINS
-----------------
Opening the fleet intermittently showed a yellow bar across the bottom of the
window, a preview of some unrelated session, and pressing Enter dropped the
operator into that session instead of the fleet picker.

The yellow was never a pane mode. On this server ``status-style`` is
``bg=green`` and ``message-style`` is ``bg=yellow``, so a yellow bottom line is
a tmux MESSAGE. The door probed for a free picker-session name with
``has-session``, and on tmux "no such session" is an ERROR — which is a
server-side message painted on every attached client's status line. The
``2>/dev/null`` in the door silenced the door's OWN stderr and did nothing
about that.

The probe runs on the SUCCESS path (the picker name is free every time), so the
message fired on every single connection. ``display-time`` is 750ms, so a warm
connect finished painting after it expired and a cold one did not — the
intermittency. The chooser was open underneath the whole time, and the keypress
meant to dismiss the message was eaten by ``choose-tree`` as "choose the
selected item".

MEASURED with the real door under a shimmed tmux, one client attached:
unfixed 2 messages ("can't find session: lobby-<pid>" and "can't find session:
lobby"), fixed 0.

WHY THE ATTACHED CLIENT IS NOT OPTIONAL
---------------------------------------
tmux records these messages against a CLIENT. With none attached, the broken
and the fixed probe are both silent and the test proves nothing — a green that
means "I measured in the wrong state". The fixture attaches a real client over
a pty for exactly that reason, and asserts it did.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOOR = REPO_ROOT / "scripts" / "lobby-door.sh"

# Scoped to the BEHAVIOURAL test, not the module. A module-level `pytestmark`
# applies to every test in the file, so it also skipped the static companion —
# which needs neither tmux nor script(1), and whose entire purpose is to hold
# the line on a runner that lacks them. The guard was disabling the guard.
_needs_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None or shutil.which("script") is None,
    reason="needs tmux and script(1) to attach a real client",
)


def _tmux(sock: str, *args: str, **kw):
    env = dict(os.environ)
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = "/tmp"
    return subprocess.run(
        ["tmux", "-L", sock, *args], env=env, capture_output=True, text=True, timeout=60, **kw
    )


def _miss_count(sock: str) -> int:
    out = _tmux(sock, "show-messages").stdout
    return out.count("can't find session")


@_needs_tmux
def test_the_door_adds_no_tmux_message_to_an_attached_client(tmp_path):
    sock = f"lobbyprobe{os.getpid()}"
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    shim = shim_dir / "tmux"
    shim.write_text(f'#!/bin/sh\nexec {shutil.which("tmux")} -L {sock} "$@"\n')
    shim.chmod(0o755)

    env = dict(os.environ)
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = "/tmp"
    env["PATH"] = f"{shim_dir}:{env['PATH']}"

    client = None
    try:
        for n in (1, 2, 3):
            _tmux(sock, "new-session", "-d", "-s", f"cc-{n}", "sleep", "120")
        time.sleep(0.5)

        client = subprocess.Popen(
            ["script", "-qec", f"tmux -L {sock} attach -t cc-1", "/dev/null"],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(2)

        # Guard-the-guard: without an attached client this test is vacuous,
        # because tmux keeps these messages per client.
        clients = _tmux(sock, "list-clients").stdout.strip()
        if not clients:
            pytest.skip(f"could not attach a client on this runner: {clients!r}")

        before = _miss_count(sock)
        # The door ENDS in `exec tmux … choose-tree`, which is interactive and
        # never returns, so it MUST be bounded here or this test hangs rather
        # than fails. Bounding it is safe: both probes and the session create
        # happen before that exec, and the new-session assertion below proves
        # they ran rather than assuming it.
        subprocess.run(
            ["timeout", "5", "script", "-qec", str(DOOR), "/dev/null"],
            env=env,
            capture_output=True,
            timeout=60,
        )
        time.sleep(0.5)

        # Guard-the-guard: the door must actually have RUN, or a zero delta
        # below means nothing happened rather than nothing was emitted.
        log = _tmux(sock, "show-messages").stdout
        assert "new-session -s lobby" in log, (
            "the door did not run under the shim; a zero message count proves "
            f"nothing.\nmessages:\n{log[-2000:]}"
        )

        added = _miss_count(sock) - before
        assert added == 0, (
            f"the door made tmux emit {added} 'can't find session' message(s), "
            "each of which paints every attached client's status line yellow "
            "(message-style bg=yellow). Use an existence test that does not "
            "error on the success path.\n"
            + "\n".join(ln for ln in log.splitlines() if "can't find session" in ln)
        )
    finally:
        if client is not None:
            client.kill()
        _tmux(sock, "kill-server")


def test_the_door_uses_no_erroring_session_probe():
    """Static companion: runs anywhere, and names the shape to avoid.

    The behavioural test above needs tmux and a pty. This one holds the line on
    a runner that has neither, and states the rule in the failure message so the
    next author does not have to re-derive it.
    """
    code = [ln for ln in DOOR.read_text().splitlines() if not ln.lstrip().startswith("#")]
    offenders = [ln.strip() for ln in code if "has-session" in ln]
    assert not offenders, (
        "lobby-door.sh probes session existence with `has-session`, which on "
        "the FREE-name path makes tmux emit 'can't find session' — a "
        "server-side message shown yellow on every attached client's status "
        "line. `2>/dev/null` does not suppress it. Offending line(s): "
        f"{offenders}"
    )
