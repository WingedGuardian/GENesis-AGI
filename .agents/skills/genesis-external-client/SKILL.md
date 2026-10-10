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

The committed Codex configuration currently selects the health and recall floor.
The launcher also supports the reviewed, opt-in `--profile interactive` role:
34 memory and 84 health tools, including directed memory writes and existing
browser, queue, campaign, settings and cognitive tools. `document_delete` and
nine health tools remain deferred; future tool names require capability review.
See `docs/reference/codex-interactive-mcp.md` for scope and qualification limits.

The standalone launcher enforces `--external-client` on the server as well as
the client allowlist. Both the `external` default and explicit `validator`
profile currently expose the same health and recall floor. Direct calls to
other tools are rejected before their bodies run; the health bootstrap also
skips dispatch queue and campaign initialization. The interactive role reuses
ordinary health initialization without starting a worker or executor. Startup
and lazy router retries cannot restore scrubbed session markers from secrets.env,
and explicit environment values win over file values for external clients.
These tool profiles are not
authentication or isolation from an operator with full host access.

Tool availability does not grant permission to publish, pay providers, dispatch
work, change settings, or modify private records. Apply the existing Genesis
rules and the user's explicit request. Memory writes use existing storage and
retrieval semantics: an ID can name durable SQLite/FTS content while vector
indexing is pending. Verify the appropriate fresh reader before claiming success.
Do not assume a failed or cancelled mutation rolled back, or automatically retry
an ambiguous remote operation. Credential references retain existing explicit
lookup auditing and recall behavior; this role adds no credential partition.

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

The project also wires a local Codex CLI shell action guard for review budgets.
It denies actions requiring fresh approval and requests user handoff, without
registering a Genesis session or persisting approval receipts. See
`docs/reference/codex-review-stop.md` for activation, tested failures, scope and
integration limits; this is not universal client enforcement.
