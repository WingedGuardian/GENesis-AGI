- **The graph engine can now answer, and nothing routes to it yet.** A second
  memory-graph backend lands behind the existing seam: a long-lived FalkorDB
  server reached over a unix socket, with its own projection of the memory
  graph. **Reads do not move** — the selector in `config/graphstore.yaml`
  defaults to the in-process NetworkX store, and every unreadable or
  unrecognised value degrades to it as well. Moving reads onto the new engine is
  a deliberate one-line change; nothing moves them there by accident.

  What it buys, when you do move: NetworkX rebuilds its whole projection
  whenever any writer invalidates it, and the new store never rebuilds. It also
  re-checks whether a memory has expired on every read instead of once per
  rebuild, so a memory whose validity window closes becomes invisible
  immediately rather than waiting for the next write to trigger a rebuild.

  What it does not change: both stores hide exactly the same memories — 400 real
  traversals were replayed through both and disagreed on none of them, which is
  the point, since which backend answers must never change what you are shown.
  Betweenness centrality deliberately stays on NetworkX whichever backend is
  selected; the new engine cannot compute it, and quietly substituting a
  different measure would change which memories are protected from
  consolidation. If the engine is unreachable, traversals fall back to NetworkX
  and then to SQL rather than returning an empty answer that would read as "this
  memory has no connections".

  If you do arm the engine, build its projection first with `python -m
  genesis.memory.graphstore_project` — safe to re-run at any time, including
  while the engine is being read, because the new copy is built alongside the
  old one and swapped in atomically. Selecting the engine without a projection
  is refused rather than silently answered, since an empty graph and a memory
  with no connections are indistinguishable from the answer alone.

  **This update does change your dependencies.** The graph engine is part of the
  memory architecture rather than an optional add-on, so its client ships as a
  normal dependency — which also raises `redis` from 7.x to 8.x, because the
  client requires it. Genesis itself never connects to redis outside the graph
  engine; the version matters only because other libraries import it. Before
  shipping this, that upgrade was tested against the stack that does: the MCP
  server framework and its task/queue dependency import and run correctly under
  redis 8, and the full graph projection was built and queried on it. Nothing to
  do on your side beyond the usual update.
