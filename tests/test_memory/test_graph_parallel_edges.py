"""Parallel typed edges must survive the graph load.

`memory_links`' production primary key is ``(source_id, target_id, link_type)``
(migration 0029: "distinct relationship types between the same pair … must
coexist, not silently overwrite"), so ONE memory pair can legitimately carry
several typed edges.

`graph.py` loaded those rows into an ``nx.DiGraph``, which cannot hold parallel
edges: the second ``add_edge`` for a pair OVERWRITES the first one's attributes.
The graph therefore kept an ARBITRARY survivor per pair, and the survivor is
what ``_bfs_with_strength``'s ``min_strength`` / ``link_type_filter`` checks
were evaluated against — so a weak or wrong-typed survivor could hide a
neighbour that a strong, matching parallel edge really does reach.

MEASURED on the live database (2026-09-02): 142 edges lost across 139 multi-type
pairs, and 0 of those pairs straddled a live ``min_strength`` threshold. So no
recall result is known to be wrong today; these tests lock the correctness
property before an engine migration builds on the loader, and they fail on the
pre-fix code. (Row and pair counts track a growing table — 252,525 rows at the
first measurement, 252,773 hours later — so re-derive them rather than quoting
these numbers.)

Traversal-wide label determinism WAS a separate, unfixed property when this
module was written, and is no longer: the multi-parent tests at the end of the
file cover it. A node reachable from several parents used to be claimed by
whichever one the queue reached first — following row order, and reporting the
weaker edge in 6.24% of cases — and now reports the strongest edge from any
nearest parent, identically under any row order.

Note the fixture below uses the PRODUCTION primary key. The pre-existing
fixture in ``test_graph.py`` declares ``PRIMARY KEY (source_id, target_id)``,
which structurally forbids the shape under test — which is precisely why the
existing suite could never have caught this.
"""

from __future__ import annotations

import aiosqlite
import pytest

from genesis.memory.graph import invalidate_graph_cache, traverse

pytestmark = pytest.mark.asyncio


# Single-sourced so the two builders below cannot drift apart.
_SCHEMA = """
    CREATE TABLE memory_links (
        source_id   TEXT NOT NULL,
        target_id   TEXT NOT NULL,
        link_type   TEXT NOT NULL,
        strength    REAL NOT NULL DEFAULT 0.5,
        created_at  TEXT NOT NULL,
        PRIMARY KEY (source_id, target_id, link_type)
    )
"""


async def _make_db(links):
    """Build an in-memory DB holding ``links`` in the given INSERTION order."""
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(_SCHEMA)
    for src, tgt, link_type, strength in links:
        await db.execute(
            "INSERT INTO memory_links VALUES (?, ?, ?, ?, '2026-09-02')",
            (src, tgt, link_type, strength),
        )
    await db.commit()
    return db


@pytest.fixture
async def parallel_db():
    """A DB whose memory_links PK matches production (pair + link_type)."""
    # Under a DiGraph the surviving edge is whichever row loads LAST, so which
    # edge survives depends on the SELECT's row order — and the loader's query
    # carries no ORDER BY. Insertion order is NOT enough to pin that: an index
    # scan would resolve EVERY pair by the same alphabetical link_type rule, so
    # "two pairs inserted in opposite orders" still leaves both keeping the same
    # side. The link_types below are chosen so the WEAK edge sorts LAST for A→B
    # and FIRST for A→D:
    #   rowid / PK-index ASC  -> A→B keeps the weak 'supports' 0.2
    #   reversed / PK DESC    -> A→D keeps the weak 'action_item_for' 0.2
    # so at least one pair is broken under every plausible scan order, and the
    # assertions below (which require BOTH pairs correct) fail on the old
    # loader in all of them. VERIFIED across all four orders, not assumed.
    # (Two earlier revisions of this fixture were vacuous — first by insertion
    # order, then by index order — which is why the property is now pinned in
    # the DATA rather than in a comment.)
    links = [
        ("A", "B", "extends", 0.9),
        ("A", "B", "supports", 0.2),
        ("A", "D", "action_item_for", 0.2),
        ("A", "D", "extends", 0.9),
        ("A", "C", "extends", 0.8),
    ]
    db = await _make_db(links)

    invalidate_graph_cache()
    yield db
    invalidate_graph_cache()
    await db.close()


async def test_parallel_edges_both_survive_the_load(parallel_db):
    """Both typed A→B rows must exist in the loaded graph, not just one."""
    from genesis.memory.graph import _ensure_graph

    graph = await _ensure_graph(parallel_db)
    # 5 rows in, 5 edges out. A DiGraph collapses each multi-type pair to one
    # edge and yields 3.
    assert graph.number_of_edges() == 5


async def test_weak_parallel_edge_cannot_mask_a_strong_one(parallel_db):
    """min_strength must consider EVERY parallel edge, not an arbitrary one.

    A→B and A→D each carry a 0.2 and a 0.9 edge, so at min_strength=0.5 both
    are reachable via their 0.9 'extends' edge. If the loader kept only a weak
    survivor for a pair, that neighbour is wrongly filtered out entirely.
    """
    result = await traverse(parallel_db, "A", max_depth=1, min_strength=0.5)
    ids = {n.memory_id for n in result.nodes}
    assert {"B", "D"} <= ids, "a strong parallel edge was masked by a weak one"
    assert "C" in ids


async def test_reported_edge_is_the_strongest_passing_one(parallel_db):
    """The surfaced link_type/strength must not be an arbitrary parallel edge.

    Results are ordered by strength and sliced to the top 5 before reaching the
    model (mcp/memory/core.py), so reporting 0.2/'supports' when a 0.9/'extends'
    edge exists is wrong on both counts — and can reorder that slice.
    """
    result = await traverse(parallel_db, "A", max_depth=1, min_strength=0.0)
    by_id = {n.memory_id: n for n in result.nodes}
    for pair in ("B", "D"):
        assert by_id[pair].strength == pytest.approx(0.9), pair
        assert by_id[pair].link_type == "extends", pair


async def test_tied_strength_resolves_independently_of_row_order():
    """Equal-strength parallel edges on ONE pair must not resolve by row order.

    Selecting the strongest edge leaves the ties unresolved: 106 of the live
    graph's 139 multi-type pairs (MEASURED 2026-09-02) carry EQUAL strengths, so
    for those the reported ``link_type`` was still whichever row happened to load
    first — the same arbitrary-survivor defect this module exists for, merely
    narrowed from 139 pairs to 106. The loader's SELECT has no ORDER BY, so
    "first" is a query-plan accident that can differ between rebuilds.

    SCOPE — read this before trusting the name. The single root here has exactly
    one neighbour, so the property under test is the WITHIN-PAIR choice and
    nothing more. Traversal-wide label determinism is a DIFFERENT and much larger
    property, and the code now has it — but it is tested SEPARATELY, at the end of
    this file, precisely so that each failure names its own cause. Keep this root
    single-neighboured: a multi-parent fixture here would measure the other
    property and pass or fail for the wrong reason.

    This asserts the property that actually matters — the SAME answer from two
    row orders — rather than a specific winner under one order, because an
    order-specific assertion goes vacuously green the moment the planner picks a
    different scan. The pinned value is asserted as well, so the test still fails
    under an index scan (where both orders agree, but on the wrong edge).

    Honest coverage limit, MEASURED against the real ``_bfs_with_strength`` in all
    four orders rather than reasoned: only TWO discriminate. The pre-fix code kept
    the first-loaded edge, so any order that loads the alphabetically-last edge
    first already agreed with the fixed behaviour — that is BOTH descending orders
    (rowid-reversed and PK-index DESC), where pre- and post-fix are behaviourally
    identical for every tied pair and no fixture can be red. This test is red on
    a rowid scan (today's plan) via the invariance assertion, and on an ascending
    PK-index scan via the pinned value below. A tie-break policy cannot escape
    this: ties resolve on link_type, and the PK index is ordered by link_type, so
    one index direction always coincides with the policy.
    """
    tied = [
        ("A", "E", "decided", 0.7),
        ("A", "E", "supports", 0.7),
    ]
    reported = {}
    for label, links in (("forward", tied), ("reversed", list(reversed(tied)))):
        # The graph cache is module-global and this test takes no fixture that
        # clears it, so without this the FIRST load could reuse a graph left
        # behind by a preceding test. Iteration 2 is already covered by the
        # `finally` below — measured: removing this line does NOT make the test
        # pass on pre-fix code, so it guards the first load, nothing more.
        invalidate_graph_cache()
        db = await _make_db(links)
        try:
            result = await traverse(db, "A", max_depth=1, min_strength=0.0)
            reported[label] = {n.memory_id: n for n in result.nodes}["E"].link_type
        finally:
            invalidate_graph_cache()
            await db.close()

    assert reported["forward"] == reported["reversed"], (
        f"tied parallel edges resolved by row order: {reported}"
    )
    # Deterministic, not semantically ranked: max strength, then the highest
    # link_type. Lexicographic-max happens to DEMOTE 'contradicts' (it sorts
    # early), which is the safe direction — graph_expansion excludes that type
    # from LLM-visible context. MEASURED: 0 of the 106 tied pairs carries one.
    assert reported["forward"] == "supports"


# ── multi-parent claiming: the property the tie test deliberately excludes ──

# R reaches X and Y each through TWO depth-1 parents of unequal strength. Under a
# per-parent `best`, whichever parent the queue reached first claimed the child
# and ITS edge was reported — so the answer followed row order and could report
# the WEAKER edge.
#
# TWO opposite-polarity triples, not one, and the property is pinned in the DATA
# for the same reason the `parallel_db` fixture above is: a single triple is
# vacuous under half the plausible scan orders, and a comment asserting otherwise
# is exactly how that fixture went vacuous twice. Alphabetically
# ``P_far < P_near`` and ``Q_alpha < Q_omega``, and the Q pair is INSERTED
# strong-first while the P pair is inserted weak-first, so:
#
#   rowid ASC  (insertion; today's plan) -> P_near first -> X weak   BROKEN
#   rowid DESC (reversed)                -> Q_alpha first -> Y weak  BROKEN
#   PK-index ASC                         -> Q_alpha first -> Y weak  BROKEN
#   PK-index DESC                        -> P_near first -> X weak   BROKEN
#
# so at least one child is mis-reported under EVERY order and the assertions
# (which require both correct) fail on the old code in all four. VERIFIED by
# replaying all four orders against the real traversal, not reasoned.
_MULTI_PARENT_LINKS = [
    ("R", "P_near", "extends", 0.9),
    ("R", "P_far", "extends", 0.9),
    ("P_near", "X", "supports", 0.4),  # WEAK path to X — sorts LATE alphabetically
    ("P_far", "X", "supports", 0.8),  # STRONG path to X
    ("R", "Q_omega", "extends", 0.9),  # inserted FIRST of the Q pair
    ("R", "Q_alpha", "extends", 0.9),
    ("Q_alpha", "Y", "supports", 0.4),  # WEAK path to Y — sorts EARLY alphabetically
    ("Q_omega", "Y", "supports", 0.8),  # STRONG path to Y
]


async def test_multi_parent_node_reports_the_strongest_reaching_edge():
    """A node reachable from two parents must report the STRONGER edge.

    This is a correctness assertion, not a tidiness one. ``strength`` is put in
    front of the model AND is the key THIS module orders on before
    ``mcp/memory/core.py`` takes ``traversal.nodes[:5]`` unsorted, so a node
    credited with a weaker parent's edge sinks in that ordering and can drop out
    of the five entirely.

    MEASURED against the real module on the live graph, old vs new: 970 of 15,433
    reported nodes (6.29%) gained a higher, truer strength, across 48.8% of roots,
    largest single understatement 0.203. Re-derive rather than quoting — and note
    the DENOMINATOR drifts between runs even on an unchanged table, because the
    loader's SELECT has no ORDER BY and any sample drawn from node order inherits
    that. The rate is stable; the counts are not.
    """
    invalidate_graph_cache()
    db = await _make_db(_MULTI_PARENT_LINKS)
    try:
        result = await traverse(db, "R", max_depth=2, min_strength=0.3)
        by_id = {n.memory_id: n for n in result.nodes}
        for child in ("X", "Y"):
            assert by_id[child].strength == pytest.approx(0.8), (
                f"{child} was credited to the weaker parent — the strongest "
                f"reaching edge is 0.8, got {by_id[child].strength}"
            )
    finally:
        invalidate_graph_cache()
        await db.close()


async def test_multi_parent_claim_does_not_follow_row_order():
    """The reported edge for a multi-parent node must not depend on row order.

    Separate from the strength assertion above because they fail for different
    reasons and a single test would not say which. This one is the determinism
    half: the same graph loaded in two row orders must answer identically.

    Both orders are asserted to be *equal* AND to equal the strong edge, because
    equality alone goes vacuously green whenever the two orders happen to agree
    on the wrong answer — the coverage trap the tie test above documents.
    """
    seen = {}
    for label, links in (
        ("forward", _MULTI_PARENT_LINKS),
        ("reversed", list(reversed(_MULTI_PARENT_LINKS))),
    ):
        invalidate_graph_cache()
        db = await _make_db(links)
        try:
            result = await traverse(db, "R", max_depth=2, min_strength=0.3)
            by_id = {n.memory_id: n for n in result.nodes}
            seen[label] = {
                c: (by_id[c].strength, by_id[c].link_type, by_id[c].depth) for c in ("X", "Y")
            }
        finally:
            invalidate_graph_cache()
            await db.close()

    assert seen["forward"] == seen["reversed"], f"multi-parent node resolved by row order: {seen}"
    for child in ("X", "Y"):
        assert seen["forward"][child][0] == pytest.approx(0.8), (
            f"both orders agreed on the WEAKER edge for {child}: {seen}"
        )


async def test_the_cte_fallback_agrees_with_the_walk_it_stands_in_for():
    """The SQL fallback must give the SAME answer as the NetworkX walk.

    `_traverse_cte` ends with `SELECT DISTINCT target_id, link_type, depth,
    strength`. DISTINCT over all four columns keeps one row per COMBINATION, not
    per memory — so a node reached through two parents at the same depth comes
    back TWICE, once credited 0.8 and once 0.4. That is precisely the defect this
    PR fixes in the walk, left standing in the path that substitutes for it.

    Asserted as AGREEMENT rather than as "the CTE improved", because the property
    that matters is that which implementation answers cannot change what the
    model is shown. `mcp/memory/core.py` slices `traversal.nodes[:5]` without
    re-sorting, so a duplicated node both occupies two of those five slots and
    drags a false weaker strength into the order that decides them.

    Uses `_MULTI_PARENT_LINKS` deliberately — the same fixture the walk's own
    multi-parent tests use, whose two opposite-polarity triples make it
    non-vacuous under every plausible scan order.
    """
    from genesis.memory.graph import _traverse_cte

    invalidate_graph_cache()
    db = await _make_db(_MULTI_PARENT_LINKS)
    try:
        cte = await _traverse_cte(db, "R", 2, 0.3)

        seen: dict[str, int] = {}
        for node in cte:
            seen[node.memory_id] = seen.get(node.memory_id, 0) + 1
        dupes = {mid: n for mid, n in seen.items() if n > 1}
        assert not dupes, f"the fallback returned a memory more than once: {dupes}"

        by_id = {n.memory_id: n for n in cte}
        for child in ("X", "Y"):
            assert by_id[child].strength == pytest.approx(0.8), (
                f"{child} must be credited its STRONGEST reaching edge (0.8), "
                f"got {by_id[child].strength}"
            )

        # The whole answer, not just the two nodes above: same memories, same
        # depths, same strengths, same labels, same ORDER. `drift.py` consumes
        # this sequence as a ranked list, so the order is load-bearing too.
        walk = await traverse(db, "R", max_depth=2, min_strength=0.3)
        assert [(n.memory_id, n.depth, n.strength, n.link_type) for n in cte] == [
            (n.memory_id, n.depth, n.strength, n.link_type) for n in walk.nodes
        ], "the fallback and the walk disagree on the same input"
    finally:
        invalidate_graph_cache()
        await db.close()


# Two depth-1 neighbours of IDENTICAL strength, inserted in the order that makes
# insertion-order and id-order disagree. Without a total key the two
# implementations resolve this tie by different rules — the walk by memory id,
# the fallback by whatever SQLite's scan happens to yield — so the sibling test
# above passes on values while the ORDER silently diverges.
_TIED_NEIGHBOUR_LINKS = [
    ("R", "z_node", "supports", 0.9),  # inserted FIRST, sorts LAST by id
    ("R", "a_node", "supports", 0.9),
]


async def test_the_fallback_and_the_walk_order_ties_the_same_way():
    """Agreement on VALUES is not agreement on ORDER, and both are consumed.

    `drift.py:202` reads this sequence as a ranked list for RRF without reading
    a single label, so two implementations that agree on every field and
    disagree on their order still give that consumer different answers. The walk
    commits a level ordered `(-strength, memory_id)` and its final sort on
    `(depth, -strength)` is stable, so ties keep the id order.

    WHAT THIS DOES AND DOES NOT PIN, because the first version of this docstring
    got it wrong. It pins that the two implementations AGREE on tie order and
    that the order is id-ascending — so a change to either side's tie rule fails
    here. It does NOT pin that the fallback's trailing `target_id` is what
    achieves that: MEASURED, this query returns tied neighbours id-sorted with or
    without that key, because the window's `PARTITION BY target_id` already
    groups them. Removing the key leaves this test green (verified by mutation),
    and saying otherwise would describe a guard that does not exist.
    """
    from genesis.memory.graph import _traverse_cte

    invalidate_graph_cache()
    db = await _make_db(_TIED_NEIGHBOUR_LINKS)
    try:
        cte = await _traverse_cte(db, "R", 2, 0.3)
        walk = await traverse(db, "R", max_depth=2, min_strength=0.3)

        assert [n.memory_id for n in cte] == ["a_node", "z_node"], (
            "the fallback must break a strength tie by memory id, not by scan order"
        )
        assert [n.memory_id for n in cte] == [n.memory_id for n in walk.nodes], (
            "the fallback and the walk ordered a tie differently"
        )
    finally:
        invalidate_graph_cache()
        await db.close()


# ---------------------------------------------------------------------------
# The window's ORDER BY, pinned key by key.
#
# The tests above assert that the fallback AGREES with the walk, which is the
# property that matters — but they agree on fixtures where two of the window's
# three keys are never exercised. MEASURED by mutation on the whole file:
# flipping `link_type DESC` to ASC, or deleting `depth ASC` outright, left every
# test green while changing what the fallback returns. A key nothing can break
# is not pinned, so each gets a fixture that isolates it.
# ---------------------------------------------------------------------------

# One neighbour, two link types, IDENTICAL strength — so depth and strength are
# both ties and link_type alone decides. The walk maximises the
# `(strength, link_type)` TUPLE, making the lexicographically LATER type win.
_TYPE_TIE_LINKS = [
    ("R", "A", "contradicts", 0.7),  # inserted FIRST, sorts EARLIER by type
    ("R", "A", "supports", 0.7),
]


async def test_the_fallback_breaks_a_type_tie_the_way_the_walk_does():
    """`link_type DESC` is the walk's tuple-max, not an arbitrary key.

    `_bfs_with_strength` compares `(strength, edge_type)` as a tuple, so when two
    parallel edges carry equal strength the lexicographically LATER type wins.
    The window mirrors that with `link_type DESC`; ASC would mirror nothing and
    silently pick the other one.

    This is the direction that matters rather than a coin flip: `contradicts`
    sorts early, so an ASC rule would systematically surface the contradicting
    label over the supporting one for every equal-strength pair — and 106 of the
    139 live multi-type pairs carry equal strengths (MEASURED 2026-09-02, cited
    in `_bfs_with_strength`'s own comment).
    """
    from genesis.memory.graph import _traverse_cte

    invalidate_graph_cache()
    db = await _make_db(_TYPE_TIE_LINKS)
    try:
        cte = await _traverse_cte(db, "R", 2, 0.3)
        walk = await traverse(db, "R", max_depth=2, min_strength=0.3)

        assert [n.link_type for n in cte] == ["supports"], (
            "an equal-strength type tie must resolve to the tuple-max type"
        )
        assert [(n.memory_id, n.link_type) for n in cte] == [
            (n.memory_id, n.link_type) for n in walk.nodes
        ], "the fallback and the walk broke a type tie differently"
    finally:
        invalidate_graph_cache()
        await db.close()


# A neighbour reachable BOTH at depth 1 weakly and at depth 2 strongly. The walk
# can only ever report it at depth 1 — `visited` is stamped when the shallower
# level commits, so the deeper edge is never even considered.
_NEARER_BUT_WEAKER_LINKS = [
    ("R", "B", "supports", 0.4),  # depth 1, WEAK
    ("R", "C", "supports", 0.9),
    ("C", "B", "supports", 0.9),  # depth 2, STRONG — the walk never sees it
]


async def test_the_fallback_prefers_the_nearer_row_over_the_stronger_one():
    """`depth ASC` leads the window because nearest-wins outranks strongest-wins.

    The two rules only ever disagree in one shape — a node reachable nearer but
    weaker — and there the walk is unambiguous: `visited` is stamped at the
    shallower level, so the deeper edge is unreachable, not merely outranked.
    Drop `depth ASC` and the window sorts by strength first, returning the same
    memory at depth 2 with strength 0.9 where the walk says depth 1 and 0.4.

    Both fields are consumed, so the divergence is not academic: `depth` is shown
    to the model by `mcp/memory/core.py`, and `strength` is what this module
    orders on before that caller slices the first five without re-sorting.
    """
    from genesis.memory.graph import _traverse_cte

    invalidate_graph_cache()
    db = await _make_db(_NEARER_BUT_WEAKER_LINKS)
    try:
        cte = await _traverse_cte(db, "R", 2, 0.3)
        walk = await traverse(db, "R", max_depth=2, min_strength=0.3)

        by_id = {n.memory_id: n for n in cte}
        assert by_id["B"].depth == 1, "the nearer row must win over the stronger one"
        assert by_id["B"].strength == pytest.approx(0.4), (
            "reporting 0.9 means the deeper row was picked — the walk cannot reach it"
        )
        assert [(n.memory_id, n.depth, n.strength, n.link_type) for n in cte] == [
            (n.memory_id, n.depth, n.strength, n.link_type) for n in walk.nodes
        ], "the fallback and the walk disagree on nearest-vs-strongest"
    finally:
        invalidate_graph_cache()
        await db.close()


# ---------------------------------------------------------------------------
# The two anchor guards. Same generator: the anchor row emitted depth 1
# unconditionally, skipping constraints both the recursive step and the walk
# apply. One is live on real data, one is inert today.
# ---------------------------------------------------------------------------

_SELF_LINK_LINKS = [
    ("R", "R", "supports", 0.9),  # a memory linked to itself
    ("R", "A", "supports", 0.8),
]


async def test_the_fallback_does_not_return_the_root_as_its_own_neighbour():
    """A self-link must not make a memory its own related memory.

    The walk seeds `visited = {root_id}`, so it can never emit the root. The
    anchor had no such guard and returned it at depth 1 — burning one of the five
    slots `mcp/memory/core.py` shows, with the least useful possible entry.

    MEASURED on the live table: 30 of 269,757 rows are self-links, so this is a
    real divergence on real data rather than a hypothetical one.
    """
    from genesis.memory.graph import _traverse_cte

    invalidate_graph_cache()
    db = await _make_db(_SELF_LINK_LINKS)
    try:
        cte = await _traverse_cte(db, "R", 2, 0.3)
        walk = await traverse(db, "R", max_depth=2, min_strength=0.3)

        assert [n.memory_id for n in cte] == ["A"], (
            "the root came back as its own neighbour"
        )
        assert [n.memory_id for n in cte] == [n.memory_id for n in walk.nodes], (
            "the fallback and the walk disagree about a self-link"
        )
    finally:
        invalidate_graph_cache()
        await db.close()


async def test_both_paths_return_nothing_when_no_depth_was_asked_for():
    """`max_depth=0` asks for no levels, and the anchor used to give one anyway.

    The walk's `while frontier and depth < max_depth` never runs a level. The
    anchor emitted depth 1 regardless, because the depth bound is checked only in
    the recursive step. Inert today — production passes 1, 2, or the default 3,
    and no caller passes 0 — but the claim this file makes is that the two paths
    agree, and an agreement with a hole in it is worth less than the hole costs
    to close.
    """
    from genesis.memory.graph import _traverse_cte

    invalidate_graph_cache()
    db = await _make_db(_MULTI_PARENT_LINKS)
    try:
        assert await _traverse_cte(db, "R", 0, 0.3) == []
        walk = await traverse(db, "R", max_depth=0, min_strength=0.3)
        assert walk.nodes == []
    finally:
        invalidate_graph_cache()
        await db.close()
