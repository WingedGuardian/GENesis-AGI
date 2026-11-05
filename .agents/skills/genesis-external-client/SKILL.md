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

The ordinary Codex profile provides the approved health and recall floor.
The separately qualified validator profile also admits memory_store, private
follow_up_create/list/update and pr_verification_close. It has no session ledger,
charter, dispatch, browser, outreach, campaign or settings authority. Do not work
around either allowlist; another expansion needs its own capability review.

The standalone launcher enforces `--external-client` on the server as well as
the client allowlist. Both the `external` default and explicit `validator`
profile have separate closed tool sets: external retains5health/8memory tools;
validator admits9health/9memory. Direct calls to other tools are rejected before
their bodies run; the health bootstrap also
skips dispatch queue and campaign initialization. These tool profiles are not
authentication or isolation from an operator with full host access.

Validator follow-ups require user_input_needed, a stable request_key for creation
and no schedule, pin or session provenance; updates may touch eligible private
records from other clients. A public link or pending publication makes a row
ineligible. Memory writes retain the existing collection/supersedes interface and
its existing partial-failure limits. Use these writes only for authorized work.
Verification results are derived from strict evidence and an independent PR
number; the server forces preview until post-calibration operator opt-in. A
preview discharges nothing. After an uncertain real response, read stored state
before retrying; verification attempt counters are not idempotency keys. See
`docs/reference/codex-validator-writers.md`. This skill does not activate the
dedicated validator launcher or authorize deployment/calibration.

Do not pass a Codex thread or session identifier to `session_charter`,
`session_ledger_*`, or other session-bound tools. Those tools operate only on an
explicitly identified, existing Genesis session. Never create a synthetic session
or charter to make one work.

Use `genesis-health` for live status and `genesis-memory` for recall. Recall
may update Genesis retrieval-use metadata; this is expected and does not make
the conversation a Genesis session.

The project also configures `gitnexus` through `.claude/mcp/run-gitnexus`,
which enforces the repository's installed-version and Node compatibility pin.
Its explicit 15-tool catalog excludes source-editing `rename` and group-metadata
`group_sync`. The retained tools include raw Cypher and can have incidental
cache/recovery effects; this is not a side-effect-free server contract.
GitNexus reads a snapshot, so check
freshness before relying on it for current code. Linked worktrees use live
symbol tools for branch truth rather than indexing another checkout. See
`docs/reference/codex-gitnexus.md` for setup and qualification limits.

If a capability requires automatic context injection, transcript extraction,
session continuity, or background delivery into this conversation, explain that
it is unavailable to an external client. Do not add lifecycle hooks or work
around the boundary.

The project also wires a local Codex CLI shell action guard for review budgets.
It denies actions requiring fresh approval and requests user handoff, without
registering a Genesis session or persisting approval receipts. See
`docs/reference/codex-review-stop.md` for activation, tested failures, scope and
integration limits; this is not universal client enforcement.
