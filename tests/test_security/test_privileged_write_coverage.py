"""Guardrail: every privileged observation-consumer consults the WS-3 write gate.

``is_trusted_for_privileged_write`` (security/immunity.py) is a LIBRARY choke-point
— nothing structurally forces a consumer to call it, so a refactor that drops the
call, or a NEW privileged consumer that forgets it, would silently re-open the
user-model / autonomy-dispatch poisoning surface. This test pins the enumerated
consumer set.

The class was enumerated via a Serena ``find_referencing_symbols`` sweep of
``db/crud/observations.py::query`` (2026-08-18): exactly TWO privileged-write
consumers are reachable via ``observation_write`` — the user model
(``process_pending_deltas`` → USER_KNOWLEDGE.md) and the autonomy dispatcher
(``dispatch_cycle`` task_detected pickup). Every other caller is a read-for-context
path (perception/reflection/surplus), an observability read (dashboard/health/
version signals), or a test. If a THIRD privileged consumer is added, add it here
AND gate it.
"""

from __future__ import annotations

from pathlib import Path

import genesis

_SRC = Path(genesis.__file__).parent
_GATE = "is_trusted_for_privileged_write"

# (module relative path, the privileged-consumer function that MUST gate on origin)
_PRIVILEGED_CONSUMERS = [
    ("memory/user_model.py", "process_pending_deltas"),
    ("autonomy/dispatcher.py", "dispatch_cycle"),
]


def test_privileged_consumers_reference_the_write_gate():
    for rel, func in _PRIVILEGED_CONSUMERS:
        src = (_SRC / rel).read_text()
        assert _GATE in src, (
            f"{rel} must consult {_GATE}: its privileged consumer {func}() "
            f"auto-writes state from observation content and must bar untrusted "
            f"origins (WS-3 poisoning gate)."
        )


def test_gate_helper_exists_and_is_importable():
    from genesis.security.immunity import is_trusted_for_privileged_write

    # The invariant the consumers rely on: owner/first_party trusted, all else not.
    assert is_trusted_for_privileged_write("first_party") is True
    assert is_trusted_for_privileged_write("external_untrusted") is False
    assert is_trusted_for_privileged_write(None) is False
