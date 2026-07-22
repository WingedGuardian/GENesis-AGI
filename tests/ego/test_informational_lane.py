"""Informational proposal lane — acknowledge-only eval rows are not approvals.

Covers the shared taxonomy (INFORMATIONAL_ACTION_TYPES / is_informational /
partition_informational) and its invariants against the dispatch blocklist.
"""

from genesis.ego.session import _NEVER_DISPATCH_ACTION_TYPES
from genesis.ego.types import (
    INFORMATIONAL_ACTION_TYPES,
    is_informational,
    partition_informational,
)


def test_informational_set_contents():
    assert set(INFORMATIONAL_ACTION_TYPES) == {"j9_regression", "gauntlet_regression"}


def test_is_informational():
    assert is_informational("j9_regression") is True
    assert is_informational("gauntlet_regression") is True
    assert is_informational("investigate") is False
    assert is_informational("cell_promotion") is False  # decision-carrying
    assert is_informational(None) is False


def test_informational_is_strict_subset_of_never_dispatch():
    """Every informational type is never-dispatch, but not vice-versa —
    decision-carrying types (cell_promotion, autonomy_earnback, …) are
    never-dispatch yet still belong on the approval queue."""
    info = set(INFORMATIONAL_ACTION_TYPES)
    never = set(_NEVER_DISPATCH_ACTION_TYPES)
    assert info < never  # strict subset
    assert {"cell_promotion", "autonomy_earnback"} <= (never - info)


def test_partition_preserves_order_and_splits():
    proposals = [
        {"id": "a", "action_type": "investigate"},
        {"id": "b", "action_type": "j9_regression"},
        {"id": "c", "action_type": "maintenance"},
        {"id": "d", "action_type": "gauntlet_regression"},
    ]
    approval, informational = partition_informational(proposals)
    assert [p["id"] for p in approval] == ["a", "c"]
    assert [p["id"] for p in informational] == ["b", "d"]


def test_partition_empty():
    assert partition_informational([]) == ([], [])


def test_partition_missing_action_type_is_approval():
    # A row with no action_type is not informational → stays an approval item.
    approval, informational = partition_informational([{"id": "x"}])
    assert [p["id"] for p in approval] == ["x"]
    assert informational == []


async def test_bulk_approve_all_pending_skips_informational():
    """`approve all pending` (cross-batch resolve) must not sweep acknowledge-only
    eval rows into a resolution."""
    import aiosqlite

    from genesis.db.crud import ego as ego_crud
    from genesis.db.schema import create_all_tables
    from genesis.ego.proposals import ProposalWorkflow

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await ego_crud.create_proposal(
        conn,
        id="real1",
        action_type="investigate",
        content="real proposal",
        batch_id="batch_a",
    )
    await ego_crud.create_proposal(
        conn,
        id="j9a",
        action_type="j9_regression",
        action_category="memory",
        content="eval notice",
        ego_source="j9_eval",
    )
    try:
        results = await ProposalWorkflow(db=conn).resolve_all_pending_proposals(
            "approved",
        )
        assert "real1" in results
        assert "j9a" not in results
        assert (await ego_crud.get_proposal(conn, "real1"))["status"] == "approved"
        # Informational row untouched — still pending, never acknowledged.
        assert (await ego_crud.get_proposal(conn, "j9a"))["status"] == "pending"
    finally:
        await conn.close()
