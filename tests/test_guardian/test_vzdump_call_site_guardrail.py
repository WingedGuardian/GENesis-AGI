"""Coverage guardrail: vzdump mutation verbs stay inside the approved path.

The vzdump start/prune surface is approval-gated by CONVENTION (approval is
the caller's job; the execute layer only re-checks the gate). That convention
only holds if no new call site sneaks in outside the audited chain:

    MCP tool / dashboard RPC → outreach.rpc → provisioning.container
        → GuardianRemote → gateway verb → __main__ → provisioning.flow
        → ProxmoxAdapter

Any other caller of the mutating entry points is a finding, not a style nit —
it would bypass owner approval. Sweep the live tree, don't trust a list.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "genesis"

# The full audited chain (module → why it may touch the verb).
_ALLOWED = {
    "guardian/provisioning/proxmox.py",     # the adapter itself
    "guardian/provisioning/base.py",        # ABC defaults
    "guardian/provisioning/flow.py",        # execute core (post-approval)
    "guardian/provisioning/container.py",   # approval coordinator
    "guardian/remote.py",                   # gateway client
    "guardian/__main__.py",                 # host verb (gateway-invoked)
    "outreach/rpc.py",                      # pipeline approval shim
    "mcp/outreach_mcp.py",                  # MCP tool (approval via pipeline)
    "dashboard/routes/provision.py",        # RPC bridge (approval via pipeline)
}

_MUTATORS = re.compile(
    r"\.(vzdump_start|prune_backups|request_vzdump_start)\s*\("
    r"|execute_vzdump_start\s*\(",
)


def test_vzdump_mutators_only_in_the_audited_chain():
    offenders: list[str] = []
    for py in SRC.rglob("*.py"):
        rel = str(py.relative_to(SRC))
        if rel in _ALLOWED:
            continue
        text = py.read_text(errors="replace")
        for m in _MUTATORS.finditer(text):
            line = text.count("\n", 0, m.start()) + 1
            offenders.append(f"{rel}:{line}: {m.group(0)}")
    assert not offenders, (
        "vzdump mutation verbs outside the audited approval chain "
        "(add the call site to the chain deliberately or remove it):\n"
        + "\n".join(offenders)
    )
