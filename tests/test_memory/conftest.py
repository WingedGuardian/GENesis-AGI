"""Shared fixtures for the memory suite.

The graph facade holds its store in a MODULE GLOBAL, and that store keeps a
strong reference to the last aiosqlite connection it built its projection from.
A test that closes its connection therefore leaves a closed one reachable, and
each of those carries a Thread — so across a long session the suite accumulates
them. `graph._reset_store_for_tests()` exists to drop that reference.

It lives HERE rather than as a per-module fixture (the shape
`memory/health.py::_reset_top_tags_state` uses in test_health.py) because the
facade is driven from four modules today and nothing would make a fifth
remember. The obligation belongs at a chokepoint the callers cannot bypass,
which is what a conftest is; the alternative is a convention that holds until
someone adds a test file.

Found by review: the reset helper had ZERO callers, so the leak it was written
to prevent was happening anyway. A helper nobody calls is indistinguishable
from one that does not exist.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _reset_graph_store():
    """Drop the facade's cached store and its pinned connection between tests.

    Imported inside the fixture, not at module scope: `genesis.memory.graph`
    pulls in networkx, and a conftest import would make every test in this
    directory pay for it even when the graph is never touched.
    """
    from genesis.memory import graph as graph_mod

    graph_mod._reset_store_for_tests()
    yield
    graph_mod._reset_store_for_tests()
