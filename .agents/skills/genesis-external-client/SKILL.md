---
name: genesis-external-client
description: Use when Codex uses Genesis capabilities through MCP. Covers the owner-mode tool surface, approvals, the unattended floor, and the session-lifecycle limits that still apply.
---

# Genesis from Codex

An owner-operated Codex session gets the same Genesis MCP servers and tools a
Claude Code session gets: health, memory, outreach and recon, with no client
allowlist (owner ruling, 2026-11-08). `scripts/codex_owner_mcp.py` starts the
ordinary server and labels it `codex`, so every tool call is recorded in
`client_tool_calls` and credential lookups are stamped `client=codex`, once the
database has that table and column (migration `20261108071709`; until it runs,
calls are served and a warning is logged instead). The label
is a record of which client acted, never a permission. In a Git worktree the
launcher resolves Genesis's live runtime state through the main checkout.

Approvals: read tools are pre-approved; anything that writes, sends, spends or
spawns asks before it runs, and so does any tool not yet classified in
`.codex/config.toml`. That holds only under an approval policy that can ask:
started with `--dangerously-bypass-approvals-and-sandbox`, Codex runs every
tool without asking (measured, codex-cli 0.166.0), as Claude Code does under its
skip-permissions mode. Genesis's own gates (the payment tap, outreach rules,
board and contributor approvals) still apply on top. `browser_fill_card` asks
first like any spending tool; the payment vault's peer check may also refuse a
Codex-started server, and the owner's per-payment Telegram tap is the control
that always applies.

A Codex run that Genesis starts unattended (the executor's verifier, the
contribution reviewer) carries the dispatched marker and keeps the external
floor: 5 health and 8 memory read tools, no outreach or recon server.

Codex defers MCP schemas until requested. Before calling a Genesis tool that is
not already visible, use MCP tool discovery for its exact `server.tool` name.

This conversation is still not a registered Genesis session: Genesis does not
heartbeat, extract, summarize or inject context into it. Do not pass a Codex
thread or session identifier to `session_charter`, `session_ledger_*`, or other
session-bound tools, and never create a synthetic session or charter to make one
work; registering Codex conversations is a later phase.

The separately qualified validator profile (its own launcher, not this config)
admits memory_store, private follow_up_create/list/update and
pr_verification_close on top of the floor.

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

Validator batches also follow `docs/reference/validator-tracking.md`: preflight
the actual exposed tools, persist measurements, verify structured task receipts,
and checkpoint the selected rows. Public-by-default includes install-local and
security-related work; private tasks require concrete unavoidable disclosure
harm. A missing private writer does not block safe public filing. Honor pauses
without creating a Genesis session or adding lifecycle hooks.

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
it is not available to Codex yet (the session-lifecycle phase). Do not add
lifecycle hooks or work around the boundary.

The project also wires a local Codex CLI shell action guard for review budgets.
It denies actions requiring fresh approval and requests user handoff, without
registering a Genesis session or persisting approval receipts. See
`docs/reference/codex-review-stop.md` for activation, tested failures, scope and
integration limits; this is not universal client enforcement.
