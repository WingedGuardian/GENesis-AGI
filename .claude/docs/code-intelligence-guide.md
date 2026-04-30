# Code Intelligence — Decision Guide

Three tools for code understanding. Each has a sweet spot — use the
lightest tool that answers the question.

## When to Use Which

| Question | Tool | Why |
|----------|------|-----|
| "Find all files containing X" | **Grep** | Text pattern, universal |
| "What's the definition of this class?" | **Serena** | Type-aware, exact |
| "Who references this function?" | **Serena** | LSP find-references |
| "What breaks if I change X?" | **GitNexus** `impact` | Multi-hop graph traversal |
| "How does task dispatch work end-to-end?" | **GitNexus** processes | Execution flow tracing |
| "Which modules are tightly coupled?" | **GitNexus** clusters | Community detection |
| "What changed in my uncommitted code?" | **GitNexus** `detect_changes` | Diff → symbol mapping |
| Config, YAML, SQL, shell scripts | **Grep** | Non-Python, no LSP |
| Safe rename across codebase | **Serena** (Python) or **GitNexus** (multi-lang) | Graph-aware |
| "What API routes exist and who handles them?" | **GitNexus** `route_map` | Route → handler mapping |
| "What MCP tools are defined and where?" | **GitNexus** `tool_map` | Tool → handler mapping |

## Grep / Glob

Always available. No setup.

```
Grep: text patterns, regex, any file type
Glob: find files by name pattern
```

## Serena (LSP via Pyright)

Python-only. Type-aware symbol resolution. `mcp__serena__*` MCP tools.

**Best for:** definitions, references, type hierarchies, safe refactoring.
**Not for:** non-Python files, mocked code, config/YAML/SQL.

Key tools:
- `find_symbol(name_path, include_body=True)` — definition + body
- `find_referencing_symbols(name_path)` — all callers/users
- `get_symbols_overview(relative_path)` — file structure
- `rename_symbol` / `replace_symbol_body` — safe refactoring

Config: `.serena/project.yml`

## GitNexus (Knowledge Graph)

Graph of ~34K nodes and ~51K edges. CLI: `gitnexus <command>`.
Index: `.gitnexus/lbug` (LadybugDB). Refresh: `gitnexus analyze`.

**Best for:** impact analysis, execution flows, coupling, routes, tools.
**Not for:** text search (FTS broken on Linux — LadybugDB/ladybug#430).

### Graph Structure

**Node types:** File, Folder, Function, Class, Method, Interface,
Property, Constructor, Route (126), Tool (80), Process (224),
Community (629), plus Struct, Enum, Trait, Impl, etc.

**Edge types** (all stored as `CodeRelation` with a `type` property):

| Edge type | Count | Meaning |
|-----------|-------|---------|
| DEFINES | 29K | File defines symbol |
| CALLS | 6.7K | Function/method calls another |
| HAS_METHOD | 5.7K | Class has method |
| CONTAINS | 3.9K | Folder contains file |
| MEMBER_OF | 2.3K | Symbol belongs to community |
| HAS_PROPERTY | 1.9K | Class has property |
| STEP_IN_PROCESS | 861 | Symbol is step in execution flow |
| IMPORTS | 153 | File imports from file |
| ACCESSES | 130 | Read/write field access |
| HANDLES_ROUTE | 126 | Function handles API route |
| HANDLES_TOOL | 80 | Function handles MCP tool |
| ENTRY_POINT_OF | 49 | Function is entry point of process |
| EXTENDS | 23 | Class extends another |

### CLI Tools

```bash
gitnexus impact <symbol>              # Blast radius + risk
gitnexus impact <UID>                 # Unambiguous (use UID from context)
gitnexus context <symbol>             # 360° view: callers, callees, processes
gitnexus detect-changes               # Git diff → affected symbols/flows
gitnexus cypher "<query>"             # Raw Cypher against the graph
gitnexus query "<concept>"            # Search (degraded without FTS)
```

### MCP-Only Tools (no CLI equivalent)

These are available when GitNexus runs as an MCP server:
- `route_map` — API routes → handler functions → middleware
- `tool_map` — MCP tool definitions → handler files
- `shape_check` — API response shape vs consumer expectations
- `api_impact` — Combined route + shape + impact analysis

### MCP Resources (Low-Token Reads)

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/GENesis-AGI/context` | Overview + index freshness |
| `gitnexus://repo/GENesis-AGI/clusters` | All functional areas |
| `gitnexus://repo/GENesis-AGI/processes` | All execution flows |
| `gitnexus://repo/GENesis-AGI/process/{name}` | Step-by-step trace |
| `gitnexus://repo/GENesis-AGI/schema` | Graph schema for Cypher |

### Cypher Query Syntax

LadybugDB Cypher differs from Neo4j. Key differences:

**Edges use a single `CodeRelation` table with a `type` property:**
```cypher
-- CORRECT: filter by type property
MATCH (a)-[r:CodeRelation {type: 'CALLS'}]->(b)
WHERE a.name = 'TaskDispatcher'
RETURN b.name, b.filePath

-- WRONG: Neo4j-style named edge labels
MATCH (a)-[:CALLS]->(b)  -- ERROR: Table CALLS does not exist
```

**Common queries:**
```cypher
-- Who calls a specific method?
MATCH (caller)-[r:CodeRelation {type: 'CALLS'}]->(callee)
WHERE callee.name = 'submit'
  AND callee.filePath = 'src/genesis/autonomy/dispatcher.py'
RETURN caller.name, caller.filePath

-- All MCP tool handlers
MATCH (t:Tool) RETURN t.name, t.filePath

-- All API routes
MATCH (r:Route) RETURN r.name, r.filePath

-- Classes that extend another
MATCH (child)-[r:CodeRelation {type: 'EXTENDS'}]->(parent)
RETURN child.name, parent.name, child.filePath

-- Execution flow steps
MATCH (p:Process)-[r:CodeRelation {type: 'STEP_IN_PROCESS'}]->(s)
RETURN p.label, s.name, s.filePath
LIMIT 20
```

**Impact with disambiguation:** When `impact` returns `ambiguous` with
multiple candidates, use the `target_uid` from the candidates list:
```bash
gitnexus impact "Method:src/genesis/autonomy/dispatcher.py:TaskDispatcher.submit#3"
```

### Known Limitations

- **FTS/text search broken on Linux.** LadybugDB v0.15.x segfaults on
  `CREATE_FTS_INDEX` with 1000+ rows. Inherited from archived KuzuDB.
  Tracked: LadybugDB/ladybug#430, GitNexus#1160.
- **Vector/embedding search not configured.** Requires `--embeddings`
  flag at analyze time + embedding provider.
- **`query` tool returns empty results** without FTS. Use `context`,
  `impact`, and Cypher queries instead.
- **Env var required:** `GITNEXUS_LBUG_EXTENSION_INSTALL=never` must be
  set to prevent segfault during `gitnexus analyze`. Set in
  `~/.local/bin/env`.

### Skill Files (Detailed Workflows)

| Task | Skill |
|------|-------|
| "How does X work?" | `gitnexus-exploring` |
| "What breaks if I change X?" | `gitnexus-impact-analysis` |
| "Why is X failing?" | `gitnexus-debugging` |
| Rename / extract / refactor | `gitnexus-refactoring` |
| Tools, resources, schema | `gitnexus-guide` |
| Index, status, clean | `gitnexus-cli` |
