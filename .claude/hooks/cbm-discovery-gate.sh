#!/usr/bin/env bash
# Code intelligence discovery gate — soft nudge for code exploration.
#
# Fires on PreToolUse for Read/Grep/Glob. Suggests codebase-memory-mcp,
# Serena, or GitNexus for code files. Silently passes through for config,
# docs, and non-code files.
#
# NEVER blocks (always exit 0). Uses additionalContext for soft tips.
# Nudges once per session via sentinel file.
#
# Replaces the global ~/.claude/hooks/cbm-code-discovery-gate which
# hard-blocked (exit 2) every Read/Grep call regardless of file type.

set -u

# ── Session-once gate ──────────────────────────────────────────────────
# CC spawns each hook as a new bash process, so $$ and $PPID differ per
# invocation. Use $CLAUDE_SESSION_ID (set by CC) when available, else
# fall back to a project-dir + date hash for once-per-day-per-project.
if [[ -n "${CLAUDE_SESSION_ID:-}" ]]; then
    GATE_KEY="$CLAUDE_SESSION_ID"
else
    GATE_KEY="$(echo "${PWD:-unknown}-$(date +%Y%m%d)" | md5sum | cut -c1-16)"
fi
GATE="/tmp/cbm-discovery-gate-${GATE_KEY}"
if [[ -f "$GATE" ]]; then
    exit 0  # already nudged this session
fi

# Clean up old gate files (>1 day)
find /tmp -maxdepth 1 -name 'cbm-discovery-gate-*' -mtime +1 -delete 2>/dev/null || true

# ── Parse stdin to determine the target file ───────────────────────────
INPUT=$(cat)
TOOL_NAME=$(printf '%s' "$INPUT" | jq -r '.tool_name // empty' 2>/dev/null || true)
FILE_PATH=""

case "$TOOL_NAME" in
    Read)
        FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.file_path // empty' 2>/dev/null || true)
        ;;
    Grep)
        FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.path // empty' 2>/dev/null || true)
        ;;
    Glob)
        FILE_PATH=$(printf '%s' "$INPUT" | jq -r '.tool_input.path // empty' 2>/dev/null || true)
        ;;
esac

# ── Skip nudge for non-code files ─────────────────────────────────────
# Config, docs, markdown, JSON, YAML, shell, text, TOML, env files
# are best handled by Grep/Read directly — no nudge needed.
if [[ -n "$FILE_PATH" ]]; then
    case "$FILE_PATH" in
        *.md|*.json|*.yaml|*.yml|*.toml|*.txt|*.sh|*.bash|*.env|*.cfg|*.ini|*.conf|*.csv|*.lock|*.sql)
            exit 0  # non-code file, pass through silently
            ;;
        */plans/*|*/.claude/*|*/settings*|*/config/*|*/CLAUDE.md|*/.serena/*|*/.gitnexus/*)
            exit 0  # known config/meta paths, pass through silently
            ;;
    esac
fi

# If Grep with no path (searching whole repo), or Read on a code file,
# or Glob searching for code patterns — nudge once.
touch "$GATE"

# Emit soft tip via additionalContext (never blocks)
cat << 'TIPJSON'
{"additionalContext": "Tip: For code exploration, consider using code intelligence tools — Serena (Python symbols), codebase-memory-mcp (code graph, architecture), or GitNexus (impact analysis). See .claude/docs/code-intelligence.md for the full decision matrix."}
TIPJSON

exit 0
