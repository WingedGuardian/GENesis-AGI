"""The facade's test-reset helper is actually WIRED, not merely defined.

`graph._reset_store_for_tests()` was shipped with ZERO callers — review found
it, and a helper nobody calls is indistinguishable from one that does not
exist. The leak it was written to prevent (the module-global store pinning a
closed aiosqlite connection, each carrying a Thread, across a long session) was
happening the whole time.

The wiring is an autouse fixture in this directory's conftest. That is easy to
delete in a refactor and hard to notice, because nothing else fails when it
goes: the reset is resource hygiene, so every other test passes either way.
Hence this file — it is the only thing that turns red.
"""

from __future__ import annotations

from genesis.memory import graph as graph_mod

# The OBJECTS, deliberately, not their `id()`s.
#
# An earlier version recorded `id(...)` ints. That is unsound in both
# directions and review caught it: once the fixture replaces the module global,
# nothing else refers to the first store, so CPython is free to collect it and
# hand its address to the store allocated immediately afterwards. Two distinct
# objects then compare EQUAL and this test fails for a reason that has nothing
# to do with the wiring it guards — and the same mechanism can mask a real
# regression by coincidence. An identity test must hold what it is comparing.
#
# The retention is two objects for the lifetime of one test module, which is
# not the leak this file exists to catch: that leak is a PRODUCTION global
# pinning a closed connection across a whole session, and it is unaffected by a
# test module briefly keeping its own reference.
_seen: list[object] = []


def test_the_store_identity_is_recorded_first():
    """Half one: hold on to the store object this test saw."""
    _seen.append(graph_mod._store)


def test_the_store_is_a_different_object_in_the_next_test():
    """Half two: a fresh store means the autouse reset ran between them.

    Identity, not state: a reset that rebuilt the same object in place would
    still leave the old connection reachable, which is the thing being
    prevented. These two tests must stay in this order and in this file — they
    are one assertion split across the fixture boundary, because that boundary
    is what is under test.
    """
    _seen.append(graph_mod._store)
    assert len(_seen) == 2, "both halves must run; this test cannot stand alone"
    assert _seen[0] is not _seen[1], (
        "the store object is IDENTICAL across tests, so the autouse reset in "
        "tests/test_memory/conftest.py did not fire — _reset_store_for_tests is "
        "unwired again and the connection leak is back"
    )
