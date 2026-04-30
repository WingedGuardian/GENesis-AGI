# Code Intelligence — Decision Guide

Four tool layers for code search and analysis, lightest to richest.
Use the lightest layer that answers your question.

## Tool Layers

| Layer | Tools | Scope | Best for |
|-------|-------|-------|----------|
| **1. Text search** | Grep, Glob, Read | All files | String patterns, config, docs, non-code, known file paths |
| **2. Serena** | `mcp__serena__*` | Python only | Symbol definitions, references, type hierarchies, safe rename/refactor |
| **3. codebase-memory-mcp** | `mcp__codebase-memory-mcp__*` | 66 languages | Code graph, architecture overview, call tracing, cross-language search |
| **4. GitNexus** | `mcp__gitnexus__*` | Git + graph | Blast radius, impact analysis, execution flows, rename across codebase |

## Decision Matrix

| Question | Tool | Call |
|----------|------|------|
| "Where is this config value set?" | Grep | `Grep(pattern, glob="*.yaml")` |
| "What files match this name?" | Glob | `Glob(pattern)` |
| "Read this specific file" | Read | `Read(file_path)` |
| "Where is class X defined?" | Serena | `find_symbol(name_path="X", include_body=True)` |
| "What methods does class X have?" | Serena | `find_symbol(name_path="X", depth=1)` |
| "Who calls function Y?" | Serena | `find_referencing_symbols(name_path="Y")` |
| "What's the project architecture?" | CBM | `get_architecture(aspects=["overview"])` |
| "Find all functions related to Z" | CBM | `search_graph(name_pattern="Z")` |
| "Trace the call chain from A to B" | CBM | `trace_path(function_name="A")` |
| "What would break if I change file F?" | GitNexus | `impact(path="F")` |
| "Show execution flow through endpoint" | GitNexus | `route_map(path="src/...")` |
| "Rename symbol across the codebase" | Serena (Python) / GitNexus (any) | `rename_symbol` / `rename` |

## When Tools Overlap

- **Finding Python callers**: Serena `find_referencing_symbols` (LSP-precise)
  vs CBM `trace_path` (graph-based, cross-language). Serena is more precise
  for Python; CBM works across languages and shows the full chain.
- **Symbol lookup**: Serena `find_symbol` (type signatures, containing class)
  vs CBM `search_graph` (label/pattern-based, all languages). Use Serena
  for Python type info; CBM for broader structural search.
- **Rename**: Serena `rename_symbol` (LSP-safe, Python only) vs GitNexus
  `rename` (git-aware, any language). Serena for Python refactors;
  GitNexus when you need blast-radius awareness.

## Per-Tool Notes

**Serena** — 1-2s LSP init on first call per session, then fast. Config:
`.serena/project.yml`. Python-only (Pyright). Does not follow mock patterns
in tests. Use Grep for non-Python files.

**codebase-memory-mcp** — Tree-sitter code graph, SQLite index (~48MB for
Genesis). Supports 66 languages. 3D visualization at `localhost:9749`.
Index rebuilds automatically on each commit (post-commit hook). If stale,
run `index_repository` manually.

**GitNexus** — LadybugDB graph. `query` tool (FTS) is broken on Linux
(known issue). Other tools (`impact`, `route_map`, `api_impact`, `rename`,
`context`) work fine. Auto-reindexes on commit.

**Grep/Glob/Read** — Always available, zero overhead. Preferred for config
files, markdown, YAML, JSON, shell scripts, SQL, and any non-code content.
