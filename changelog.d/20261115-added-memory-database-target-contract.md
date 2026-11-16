Added an opt-in shared memory backend contract at `/mcp/v3` that validates the
configured logical database target before tool execution. It retains existing
transport, caller-file and bookmark rules. Ordinary frontend adoption and
mixed-database fallback remain separate; shared rollout stays off.
