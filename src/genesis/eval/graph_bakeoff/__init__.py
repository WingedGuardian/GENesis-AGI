"""Memory-graph engine bake-off harness (evaluation only — NOT runtime-wired).

Collects DATA to inform the 2026-08-14 engine decision (NetworkX-incremental vs
LadybugDB vs FalkorDB) for Genesis's memory graph. It does NOT compute a winner:
the findings doc reports raw numbers; the owner and Genesis make the subjective
call jointly (decision protocol, 2026-08-06).

Design invariants:
- Read-only against a FROZEN snapshot (every number cites the snapshot sha256).
- The pre-registered workload (``queries.py``) is frozen BEFORE any engine runs,
  so scoring can't be retrofitted to results.
- A pure-SQL parity oracle (``parity.py``) is the ground truth every engine must
  match on the reachable node-id set; ``bench time`` refuses to run without a
  green parity report.
- Nothing here imports into the server runtime; production graph code
  (``genesis.memory.graph`` / ``graph_expansion``) is REUSED read-only, never edited.

See ``~/.claude/plans/yes-go-ahead-as-humming-gosling.md`` (S1 Execution Plan).
"""
