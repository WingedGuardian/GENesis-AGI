# Agent Instructions

Cross-tool agent entry point (Codex, Cursor, OpenCode, …). The canonical
project instructions live in **CLAUDE.md** — read it first; everything below
is supplementary.

## GitNexus — Code Intelligence (advisory)

This repo is indexed by GitNexus. The MCP tools (`impact`, `query`,
`context`, `explain`, `trace`, `detect_changes`) give call-graph, blast-radius,
and execution-flow answers that grep can't. Use them when they fit the
question — **none is a mandatory pre-edit gate** (see CLAUDE.md → Code
Intelligence for the tool-selection matrix; Serena is the live-symbol
default, GitNexus is snapshot-based so run `node .gitnexus/run.cjs analyze`
first when freshness matters).

Useful entry points:

| Tool/Resource | Use for |
|---|---|
| `impact({target, direction: "upstream"})` | multi-hop blast radius before large refactors |
| `query({search_query})` | find execution flows by concept |
| `context({name})` | callers/callees/flows for one symbol |
| `detect_changes({scope: "compare", base_ref: "main"})` | regression-scope check on a branch |
| `gitnexus://repo/GENesis-AGI/processes` | all indexed execution flows |

GitNexus doc/skill injection is disabled at the source via the committed
`.gitnexusrc` (`skipAgentsMd` + `skipSkills`) — this file is hand-curated;
`surplus/jobs/gitnexus.py` strips any marker block an rc-unaware GitNexus
version re-injects.
