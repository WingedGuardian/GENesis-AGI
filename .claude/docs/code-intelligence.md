# Code Intelligence — Decision Guide

Four tools for code search and analysis, each with a lane. Pick by the
question, not a fixed hierarchy. One freshness rule cuts across all of them:
**Serena is always live** (LSP — parses current files per query, never stale),
while **CBM and GitNexus are indexed** and drift after you pull merged PRs —
reindex before trusting them for anything load-bearing.

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

## Exploration & Planning Rule

When exploring code for extraction, coupling analysis, or planning:
- **Dependency tracing** ("what depends on X?", "how many callers?") →
  Serena `find_referencing_symbols` or GitNexus `impact`
- **Coupling analysis** ("what Genesis imports does this file have?") →
  GitNexus `context` for 360° view
- **Architecture understanding** ("how do these subsystems connect?") →
  CBM `get_architecture` or `trace_path`
- **Reading specific known files** → Direct Read/Grep

Rule: Direct reads answer "what does this code do?" but NOT "what depends
on this code?" For dependency questions, specialized tools give
higher-confidence answers faster and catch things manual reads miss.

## Per-Tool Notes

**Serena** — **always live** (LSP via Pyright; parses current files per query,
so it never goes stale — no index to rebuild). 1-2s init on first call per
session, then fast. Config: `.serena/project.yml`. Python-only. Does not follow
mock patterns in tests. Use Grep for non-Python files. Default for
symbol/reference/impact questions.

**codebase-memory-mcp** — Tree-sitter code graph, SQLite index (~48MB for
Genesis). Supports 66 languages. 3D visualization at `localhost:9749`.
Reindexed by the idle-gated runner, not inline on every commit — a commit
queues a request marker and the runner rebuilds when the box is quiet (see
below). If you need a fresh index, queue a marker; do NOT call the
`index_repository` MCP tool directly for `~/genesis` (it bypasses the lock +
host freeze — see the storm warning below).
Runs under a hard 2G memory cap (`.claude/mcp/run-codebase-memory` wraps it in
a systemd scope) because upstream v0.9.0 still leaks memory without bound on query (issue #581 open)
operations (DeusData/codebase-memory-mcp#581). If its tools suddenly error
mid-session, the instance likely hit the cap and was killed — run `/mcp` to
reconnect a fresh one; Serena/GitNexus/Grep are unaffected. Cap override:
`CODEBASE_MEMORY_MCP_MEMORY_MAX` (e.g. `4G`).

**GitNexus** — LadybugDB graph (v1.6.8). Snapshot-based: correct only when the
index matches the working tree. Its reindex fires on local commit, **not** on
`git pull` of merged PRs, so the index silently drifts after merges — a stale
`impact` is confidently wrong exactly mid-change. **Reindex (`gitnexus analyze`)
when you reach for it** for something load-bearing, and prefer Serena for live
blast-radius. `query` (FTS) may be unavailable depending on the LadybugDB
extension — it degrades gracefully (skips FTS, no crash).

**Grep/Glob/Read** — Always available, zero overhead. Preferred for config
files, markdown, YAML, JSON, shell scripts, SQL, and any non-code content.

## Worktrees Are Never Indexed

Linked git worktrees get NO CBM/GitNexus indexing — by design, not oversight.
Each worktree index builds a full separate graph (gigabytes of RAM + heavy
disk writes); three concurrent worktree indexers once saturated the
container's disk-write throttle and wedged the whole container in a D-state
I/O storm. In a worktree session, use **Serena** (live LSP, no index needed)
plus the main repo's existing CBM/GitNexus graphs.

Indexing flows through a **request queue, not per-commit spawns** (fire-and-forget
full indexes on every commit stormed the container twice in 2026-07). Triggers
(post-commit hook, setup, the gitnexus surplus job, disk_reclaim) drop a marker
via `scripts/lib/index_marker.py`; the idle-gated runner
(`genesis-code-intel.timer` → `scripts/code_intel_runner.sh`) is the ONLY thing
that consumes a marker and invokes `scripts/lib/code_intel_index.sh` — the single
entrypoint enforcing worktree-skip, a per-repo single-flight lock, memory/CPU
caps, and a **pressure watchdog** that pauses the index (cgroup freeze — the only
working I/O throttle here) under load and kills a run that can't make headway. A
guardrail test (`tests/test_scripts/test_code_intel_index.py`) fails the build on
any new raw spawn site. `CODE_INTEL_INDEX_DISABLE=1` skips indexing entirely.

**Emergency kill-switch — `genesis-code-intel-freeze`.** A managed systemd USER
unit (`scripts/code_intel_freeze.sh`) that holds BOTH single-flight locks (the
main-repo lock and the runner self-lock), so while armed nothing indexes at all:
the idle runner exits each tick untouched and a manual/hook entrypoint run exits
75 ("host-frozen — keep the marker"). Rendered by bootstrap but **not** enabled —
it is an on-demand lever. Arming stops any in-flight index scope first
(kill-then-seal), so it engages in ~a second even mid-storm:

```
systemctl --user start   genesis-code-intel-freeze    # arm now
systemctl --user enable  genesis-code-intel-freeze    # arm across reboots too
systemctl --user stop    genesis-code-intel-freeze    # disarm
```

For a single index run, `CODE_INTEL_INDEX_DISABLE=1` or stopping
`genesis-code-intel.timer` are lighter alternatives.

**Do NOT call CBM's `index_repository` MCP tool for a fresh full index of
`~/genesis`.** It runs in-process and bypasses the lock, the caps, AND the
freeze kill-switch — a from-scratch full index that way is exactly what
read-saturated the container. Queue a marker instead and let the idle runner do it:
`python3 scripts/lib/index_marker.py write --repo ~/genesis --tools both --mode fast`.

---

For deep GitNexus reference (Cypher syntax, edge types, MCP resources,
graph schema): `.claude/docs/code-intelligence-guide.md`
