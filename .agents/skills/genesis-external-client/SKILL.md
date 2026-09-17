---
name: genesis-external-client
description: Use when Codex needs Genesis capabilities through MCP without joining Genesis's session lifecycle. Covers tool selection, explicit state changes, and session-boundary limits.
---

# Genesis as an external tool client

This Codex conversation is not a Genesis session. It may call the Genesis MCP
servers on demand, but Genesis must not register, heartbeat, extract, summarize,
or otherwise manage this conversation. The external launcher removes inherited
Genesis session identity, provenance, supervision, slot, and trace context before
starting either MCP server. In a Git worktree it resolves Genesis's live runtime
state through the main checkout rather than creating worktree-local state.

Codex defers MCP schemas until requested. Before calling a Genesis tool that is
not already visible, use MCP tool discovery for its exact `server.tool` name.

The initial Codex surface provides approved health and recall tools only. It
does not expose persistent memory writes, task dispatch, browser control,
outreach, Discord, recon, campaign management, or session controls. Do not
work around this allowlist; expanding it needs a separate capability review.

Do not pass a Codex thread or session identifier to `session_charter`,
`session_ledger_*`, or other session-bound tools. Those tools operate only on an
explicitly identified, existing Genesis session. Never create a synthetic session
or charter to make one work.

Use `genesis-health` for live status and `genesis-memory` for recall. Recall
may update Genesis retrieval-use metadata; this is expected and does not make
the conversation a Genesis session.

If a capability requires automatic context injection, transcript extraction,
session continuity, or background delivery into this conversation, explain that
it is unavailable to an external client. Do not add lifecycle hooks or work
around the boundary.
