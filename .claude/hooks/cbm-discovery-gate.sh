#!/usr/bin/env bash
# Code intelligence discovery gate — soft nudge for code exploration.
#
# Fires on PreToolUse for Read/Grep/Glob. Suggests codebase-memory-mcp,
# Serena, or GitNexus for code files. Silently passes through for config,
# docs, and non-code files.
#
# NEVER blocks (always exit 0). Uses additionalContext for soft tips.
# Fires every time a non-skipped code-file Read/Grep/Glob happens —
# no session-once suppression, since the nudge has value on each
# code-discovery action. Configs, docs, and plans are skipped by the
# path/extension filter below.
#
# Replaces the global ~/.claude/hooks/cbm-code-discovery-gate which
# hard-blocked (exit 2) every Read/Grep call regardless of file type.

set -u

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

# Emit soft tip via additionalContext (never blocks)
cat << 'TIPJSON'
{"additionalContext": "Tip: For code exploration, consider using code intelligence tools — Serena (Python symbols), codebase-memory-mcp (code graph, architecture), or GitNexus (impact analysis). See .claude/docs/code-intelligence.md for the full decision matrix."}
TIPJSON

exit 0
