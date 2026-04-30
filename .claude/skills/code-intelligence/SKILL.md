---
name: code-intelligence
description: Code understanding tool selection. Use when exploring architecture,
  finding definitions, tracing call chains, assessing blast radius of changes,
  or debugging code paths in the Genesis codebase.
user-invocable: false
---

# Code Intelligence Tool Selection

| Need | Tool | Example |
|------|------|---------|
| Package/module overview | `codebase_navigate` MCP (L0→L1→L2) | "What packages exist?" |
| Symbol definition/signature | Serena `find_symbol` | "Where is Router defined?" |
| Who calls/references X | Serena `find_referencing_symbols` | "Who calls _set_output?" |
| File symbol overview | Serena `get_symbols_overview` | "What's in engine.py?" |
| What breaks if I change X | GitNexus `impact` | "Blast radius of renaming Router" |
| End-to-end execution flow | GitNexus `query` | "How does task submission work?" |
| Architecture/clusters | GitNexus `context` or clusters resource | "What's in the memory package?" |
| String pattern / non-Python | Grep | "Find all TODO comments" |
| File by name | Glob | "Find all *_hook.py files" |

## Escalation Path

Start cheap, escalate as needed:
1. `codebase_navigate` for orientation (always available, fast)
2. Serena for precise symbol work (LSP-powered, Python-only)
3. GitNexus for relationships and impact (knowledge graph, needs index)
4. Grep/Glob for everything else

## Availability

- **Serena**: Always available. 1-2s init per session.
- **GitNexus**: Requires index. Check freshness: `npx gitnexus status`.
  PostToolUse hook auto-detects staleness after commits.
- **`codebase_navigate`**: Always available (Genesis health MCP).
