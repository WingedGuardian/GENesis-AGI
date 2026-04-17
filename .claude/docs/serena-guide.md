# Serena — Decision Guide

Serena is an MCP server providing LSP-powered code intelligence via Pyright.
Available as `mcp__serena__*` tools. Complements Grep, does not replace it.

## When to Use Serena

- "Where is this class/function wired in?" → `find_referencing_symbols`
- "What's the definition and signature?" → `find_symbol` with `include_body`
- "What methods does this class have?" → `find_symbol` with `depth=1`
- Architectural traversal — dependency injection patterns, type hierarchies
- Safe refactoring — `rename_symbol`, `replace_symbol_body`

## When to Use Grep Instead

- String patterns, comments, config files, migrations, YAML/JSON
- Test files using mocks (Pyright doesn't follow mock patterns)
- Anything outside Python semantics (shell scripts, HTML templates, SQL)

## When to Use the AST Code Index

Tables: `code_modules` / `code_symbols`

- Lightweight structural queries (module counts, symbol stats)
- Proactive hook enrichment (runs every prompt — must be fast)
- Package-level summaries

## Key Behaviors

- 1-2s one-time LSP init per session, then fast
- Returns semantic context (symbol kind, type signatures, containing class)
- Distinguishes `TYPE_CHECKING` imports from runtime imports
- Config: `.serena/project.yml`
