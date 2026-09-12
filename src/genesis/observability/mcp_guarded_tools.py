"""The set of MCP tools the stale-code guard blocks on a stale subprocess.

GUARDED = "overwrite/refine-class" writes only: tools that select the EXISTING
row they overwrite via stale-able MATCHING LOGIC (embedding similarity / fuzzy
match), NOT via an exact key or a caller-supplied id. Stale code in such a tool
can silently overwrite the *wrong* row — the exact failure that motivated this
guard (``procedure_store``'s pre-#1277 similarity-matched refine bumped a
distinct procedure onto a matched row).

Explicitly OUT of scope (pass through even when stale):
- Read tools — harmless when stale.
- Append-only writes (``follow_up_create``, ``session_ledger_add``,
  ``reference_store``, ``memory_store`` — exact-content dedup, ``observation_write``
  — exact content_hash dedup): stale code can't mis-target an existing row.
- Explicit-id / exact-key updates (``follow_up_update``, ``settings_update``,
  ``campaign_update``, ``ego_goal_update`` …): the caller names the target, so
  stale field-logic applies to the intended row, never a mis-selected one.

Enforcement: ``tests/test_observability/test_mcp_guarded_tools_registry.py``
scans every ``@mcp.tool()`` body for a similarity-refine marker (``*_checked``,
``find_similar``, ``SequenceMatcher``, ``.ratio(``) and asserts the set of such
tools equals GUARDED_MCP_TOOLS — so a NEW fuzzy-matched refine writer cannot ship
without being consciously added here (or the guard silently under-covering).

Leaf module: stdlib only, no genesis imports (imported by the middleware, which
must stay import-cheap and cycle-free).
"""

from __future__ import annotations

# Currently the ONLY MCP tool that overwrites an existing row via similarity
# matching. Grows deliberately, gated by the registry test above.
GUARDED_MCP_TOOLS: frozenset[str] = frozenset({"procedure_store"})
