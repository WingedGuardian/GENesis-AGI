# Auditing Existing Capabilities — enumerate, don't spot-check

Before claiming Genesis "lacks X", "needs to add X", or is "weaker than
<external system> at X" — or before any competitive/architecture comparison —
verify by ENUMERATION, not a spot-check. **Auditing a symbol is not auditing the
stack**, and a negative from a positive search is not evidence of absence:

1. Enumerate the subsystem's full module inventory before concluding anything is absent.
2. Trace the call graph BOTH directions — mechanisms often live in the
   wrapper/caller layer, not the first symbol (CRAG lives in the MCP recall
   wrapper, not `retrieval.py`; the reranker is applied by the caller).
3. Grep by CONCEPT with several synonyms, not one symbol.
4. Verify built/enabled/disabled against RUNTIME state (env gates, server logs),
   not code presence.
5. Multi-path systems → coverage matrix (N entry points × M mechanisms); hot
   auto-fired paths often carry a thinner stack than the deep path — a gradient,
   not an absence.
6. Confidence is capped by enumeration completeness.

A 2026-06-30 competitive audit wrongly claimed Genesis lacked CRAG,
scope-before-rank, and a live reranker — all three had already shipped. Full
protocol: procedure `codebase_audit` / CC memory `audit-enumerate-not-spotcheck`.
For "does Genesis already have X", consult the subsystem map
(`docs/architecture/CURRENT.md`, via the `subsystem-map` skill) FIRST;
`.claude/skills/genesis-development/references/codebase-map.md` stays the package-level structural companion.
