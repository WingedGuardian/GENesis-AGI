- **WebSearch override: says why it fell back when another plugin rewrote the
  result.** The override calls Genesis `web_search` through Claude Code's
  normal tool pipeline, so another plugin's PostToolUse hook that replaces
  large MCP results with a summary made most searches fall back to the
  built-in, logged only as "the reply was not JSON". The log now names the
  likely cause, and `.claude/docs/web-tools-guide.md` explains how to exempt
  `web_search` from such a hook (for token-optimizer:
  `TOKEN_OPTIMIZER_ARCHIVE_EXEMPT_TOOLS`).
