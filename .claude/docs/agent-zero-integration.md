# Agent Zero Integration

Genesis runs on Agent Zero (`~/agent-zero`). Option C principle: build to
upstream's trajectory, not our current pin.

## Plugin Structure

Genesis code in `~/agent-zero/usr/plugins/genesis/` and
`~/agent-zero/usr/plugins/genesis-memory/`. Never scatter files through AZ
default directories.

## Minimal Core Patches

Every core patch is rebase liability. Document ALL in
`docs/reference/agent-zero-fork-tracking.md`.

## Server Startup Hook

`run_ui.py:init_a0()` has a 3-line Genesis bootstrap
(`DeferredTask → call_extensions("server_startup")`).

**CRITICAL INVARIANT** — without it, ALL Genesis background infra is dead.
Verify after every AZ update:
```bash
grep -n "server_startup" ~/agent-zero/run_ui.py
```

## MCP as Primary Boundary

4 MCP servers are the heavyweight integration. Logic in MCP servers; context
injection in extensions.

