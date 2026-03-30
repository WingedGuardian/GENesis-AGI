# V5: Hybrid Agent Protocol (ACP + SDK)

> **Scope**: V5 — not V3 or V4. Revisit when both ACP and the Python Agent SDK
> have matured.

## Decision (2026-03-16)

After extensive evaluation of ACP (Agent Client Protocol) and the Python Agent
SDK (`claude-agent-sdk`), we decided on **Option B: SDK behind AgentProvider
protocol** for V3, but deferred the full hybrid (ACP + SDK) to V5.

## Why V5, Not V3

### ACP Issues (v0.21.0 adapter, v0.8.1 Python SDK)

- **Bug #424**: `new_session` hangs on adapter with existing session. Multi-session
  per adapter is architecturally supported but broken in v0.21.0 static binary.
- **Zombie process leaks**: No `os.killpg()` in SDK cleanup. Confirmed by issues
  #314, #47, #338. Orphaned processes consume CC concurrency slots.
- **~11s startup per adapter** (2.4s init + 8.6s session creation).
- **System prompt works** via `_meta.systemPrompt` on `new_session()` (confirmed).
- **Model swap works** via `set_session_model()` (confirmed).
- **Provider flexibility is the key value** — 19+ agents (Codex, Gemini, Copilot,
  etc.) swappable via registry.

### Python Agent SDK Issues (v0.1.48)

- **MCP "Stream closed" after ~70s** (#676): Custom MCP tool calls rejected after
  inactivity timer. Built-in tools unaffected. BLOCKER for MCP-heavy sessions.
- **CLOSE_WAIT CPU spin after disconnect** (#665): Leaked TCP socket causes ~24%
  CPU spin in long-running processes. No full workaround.
- **rate_limit_event crashes parser** (#601/#603): Every session start emits this
  event; SDK can't parse it. Crashes async generator.
- **Session file not flushed** (#625): `disconnect()` SIGTERMs subprocess before
  session JSONL is fully written. Session resume loses last message.
- **Cross-task hang** (#576): `ClaudeSDKClient` hangs if `receive_response()` is
  called from different asyncio Task than `connect()`.
- **Session isolation broken** (#560): Different session IDs on one client share
  context. Need fresh client per session.
- **Hooks work** (dict access, not isinstance). **can_use_tool works** via
  ClaudeSDKClient. **System prompt works**. **stderr workaround works** (callback
  + debug-to-stderr flag).

### What V3 Does Instead

Stays with the current subprocess invoker (`CCInvoker`), which has none of these
issues. Adds an `AgentProvider` protocol abstraction so the SDK or ACP can be
slotted in later without changing anything above `CCInvoker`.

## V5 Revisit Criteria

Revisit the hybrid architecture when:

1. **ACP adapter bug #424 is fixed** — multi-session per adapter works
2. **Python SDK MCP stream closed (#676) is fixed** — custom MCP tools work >70s
3. **Python SDK CLOSE_WAIT (#665) is fixed** — no CPU spin after disconnect
4. **Python SDK rate_limit_event (#601) is fixed** — parser handles all event types
5. **Python SDK reaches v1.0** — stable API, no daily breaking changes
6. **Genesis actually needs a non-Claude provider** — concrete use case, not hypothetical

## Architecture for V5

```
              ┌────────────────────────┐
              │    AgentProvider        │  (protocol, defined in V3)
              │    .invoke()           │
              │    .invoke_streaming() │
              │    .interrupt()        │
              └──────┬──────────┬──────┘
                     │          │
          ┌──────────▼───┐ ┌───▼──────────┐
          │ ACP Backend  │ │ SDK Backend   │
          │ (any agent)  │ │ (Claude-only) │
          │ + system     │ │ + hooks       │
          │   prompt via │ │ + can_use_tool│
          │   _meta      │ │ + set_model() │
          │ + model swap │ │ + interrupt() │
          └──────────────┘ └──────────────┘
```

## Evaluation Artifacts

All spike scripts preserved at `scripts/spike_*.py` for re-evaluation.
Full findings documented in `.claude/plans/encapsulated-riding-pony.md`.
