Toggle Genesis context for interactive Claude Code sessions.

When Genesis context is ENABLED, interactive CC sessions receive:
- Genesis identity (SOUL.md, USER.md, CONVERSATION.md, STEERING.md)
- Cognitive state from the Genesis database
- MCP tools (genesis-health, genesis-memory, genesis-recon)

When DISABLED, CC operates as a standard dev tool with only CLAUDE.md and auto-memory.

## Usage

- `/genesis` or `/genesis status` — show current state
- `/genesis off` — disable Genesis context (takes effect on next session)
- `/genesis on` — enable Genesis context (takes effect on next session)

## Implementation

Check the flag file at `~/.genesis/cc_context_enabled`:

```bash
# Check status
if [ -f ~/.genesis/cc_context_enabled ]; then echo "ENABLED"; else echo "DISABLED"; fi
```

If the user says "off" or "disable":
1. Run: `rm -f ~/.genesis/cc_context_enabled`
2. Confirm: "Genesis context DISABLED. Start a new CC session for this to take effect. Current session retains its context."

If the user says "on" or "enable":
1. Run: `mkdir -p ~/.genesis && touch ~/.genesis/cc_context_enabled`
2. Confirm: "Genesis context ENABLED. Start a new CC session for this to take effect."

If no argument or "status":
1. Check flag file existence
2. Check if identity files exist at `src/genesis/identity/`
3. Check if `~/.genesis/status.json` exists (health data available)
4. Check if `.mcp.json` exists in project root (MCP servers configured)
5. Report all findings
