"""Regression lock: the guardian redeploy verb must always restart
``genesis-guardian.timer`` on every exit path.

Audit finding idx 24 (PR #171): with ``set -e``, ``guardian-gateway.sh`` can
exit between stopping the timer for a redeploy and the final restart (a failing
``cp``/``mv``, CLAUDE regen, or the ``deploy_state.json`` write), leaving the
host guardian silently disabled until manual intervention. It is already fixed
by an ``EXIT`` trap gated on ``_TIMER_STOPPED``; this test locks that invariant
so a future edit to the (large, hand-maintained) gateway script can't remove it
unnoticed. Structural assertions only — no shell execution, install-agnostic
(the script path is resolved relative to this test file).
"""

from __future__ import annotations

import re
from pathlib import Path

GATEWAY = Path(__file__).resolve().parents[2] / "scripts" / "guardian-gateway.sh"


def _gateway_text() -> str:
    assert GATEWAY.is_file(), f"guardian gateway script missing at {GATEWAY}"
    return GATEWAY.read_text()


def test_redeploy_stops_the_guardian_timer():
    # The timer IS stopped during redeploy — which is exactly why the restart
    # guarantee below is load-bearing.
    text = _gateway_text()
    assert re.search(r"systemctl --user stop genesis-guardian\.timer", text), (
        "redeploy no longer stops genesis-guardian.timer — invariant premise changed"
    )


def test_exit_trap_restarts_guardian_timer():
    # An EXIT trap whose body restarts the timer → the guardian can never be
    # left disabled by an early `set -e` abort mid-redeploy.
    text = _gateway_text()
    assert re.search(
        r"trap\s+'[^']*systemctl --user start genesis-guardian\.timer[^']*'\s+EXIT",
        text,
    ), "the EXIT trap that restarts genesis-guardian.timer is gone (idx 24 regression)"


def test_exit_trap_is_gated_on_timer_stopped_flag():
    # The trap only restarts if the timer was actually stopped (_TIMER_STOPPED),
    # and the flag is both raised (=1 after stop) and cleared (=0 after a clean
    # restart) so a successful redeploy doesn't double-start.
    text = _gateway_text()
    assert re.search(r'\[ "\$_TIMER_STOPPED" = 1 \]', text), (
        "EXIT trap no longer guards on _TIMER_STOPPED"
    )
    assert "_TIMER_STOPPED=1" in text, "_TIMER_STOPPED is never raised after the stop"
    assert "_TIMER_STOPPED=0" in text, "_TIMER_STOPPED is never cleared after restart"
