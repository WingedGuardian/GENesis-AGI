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
