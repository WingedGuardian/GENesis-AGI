"""Guardrail: Guardian's recovery brain must target genesis-server.

genesis-bridge is a deprecated, on-demand Telegram relay (inactive + disabled
on a normal install). genesis-server is the main service. A partial migration
left the container-side watchdog fixed but the host recovery brain still
restarting/probing/diagnosing against genesis-bridge — so recovery of an
inactive unit "succeeded", verification failed, and genesis-server crash loops
were invisible to the NRestarts probe (audit 2026-07-02 §7).

This mechanical guard fails if any recovery-brain module reintroduces a
genesis-bridge reference, preventing the drift from recurring silently.
"""

from pathlib import Path

import pytest

_GUARDIAN = Path(__file__).resolve().parents[2] / "src" / "genesis" / "guardian"

# The modules that act on / reason about "the main service".
_RECOVERY_BRAIN = [
    "recovery.py",
    "health_signals.py",
    "diagnosis.py",
    "briefing.py",
]


@pytest.mark.parametrize("module", _RECOVERY_BRAIN)
def test_recovery_brain_targets_genesis_server_not_bridge(module: str) -> None:
    src = (_GUARDIAN / module).read_text()
    assert "genesis-bridge" not in src, (
        f"{module} references the deprecated genesis-bridge unit. Guardian's "
        "recovery brain must target genesis-server (the main service); the "
        "bridge is an inactive on-demand relay. Restarting/probing it 'succeeds' "
        "but heals nothing and hides genesis-server crash loops."
    )
