"""The autonomy init must NEVER leave the proposal dispatch gate unset.

Previously, if the real ``ProposalDispatchGate`` could not be built, the
attribute stayed unset and the ego wired it as ``None`` -> every approved
proposal dispatched UNGATED (a silent fail-OPEN).  Stage-1 hardening installs
a fail-closed ``DenyHighRiskSentinel`` by default, so any non-success path
leaves a blocking gate.
"""

from __future__ import annotations

import types

from genesis.autonomy.proposal_gate import DenyHighRiskSentinel
from genesis.runtime.init import autonomy as autonomy_init


async def test_init_installs_fail_closed_sentinel_when_db_missing():
    """With no DB the real gate is never built — the sentinel must remain."""
    rt = types.SimpleNamespace(
        _db=None,
        _event_bus=None,
        _cc_invoker=None,
        _outreach_pipeline=None,
        _awareness_loop=None,
    )

    await autonomy_init.init(rt)

    # Never left unset (the ego reads a missing gate as "no gate" => bypass).
    assert isinstance(rt._proposal_dispatch_gate, DenyHighRiskSentinel)
