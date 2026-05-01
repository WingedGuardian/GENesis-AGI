#!/usr/bin/env bash
# Code intelligence session reminder — lists available tool layers.
#
# Fires on SessionStart (startup/resume/clear/compact).
# Informational only, points to the full decision guide.

cat << 'REMINDER'
Code intelligence tools available:
1. Grep/Glob/Read — text search, file patterns, configs, docs
2. Serena (mcp__serena__*) — Python LSP: symbols, references, rename
3. codebase-memory-mcp — 66-language code graph: search_graph, trace_path, get_architecture
4. GitNexus (mcp__gitnexus__*) — blast radius, impact analysis, execution flows
Decision guide: .claude/docs/code-intelligence.md
REMINDER
