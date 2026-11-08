"""SQL cycle exclusion compares complete TEXT IDs, as NetworkX does."""

import itertools

import pytest

from genesis.memory.graph import _traverse_cte
from genesis.memory.graphstore_nx import NetworkxGraphStore

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("root,middle,leaf", [
    ("root", "node-10", "node-1"),
    ("root", "CASE", "case"),
    ("root", "plain", "%"),
    ("root", "plain", "_"),
    ("root", "left,right", "right"),
    ("root", "plain", ""),
    ("root", "", "tail"),
    ("", "middle", "tail"),
    ("root", "a\0b", "a\0c"),
    ("root", "雪花", "雪"),
    ("root", "é", "e\u0301"),
])
async def test_exact_identity_preserves_distinct_nodes_and_excludes_cycles(db, root, middle, leaf):
    await db.executemany(
        "INSERT INTO memory_links (source_id,target_id,link_type,strength,created_at) "
        "VALUES (?,?,'supports',.8,'2026-01-01')",
        [(root, middle), (middle, leaf), (leaf, root), (leaf, middle), (middle, middle)],
    )
    await db.commit()
    expected = await NetworkxGraphStore().traverse(db, root, max_depth=8, min_strength=0)
    assert [n.memory_id for n in expected] == [middle, leaf]
    assert await _traverse_cte(db, root, 8, 0) == expected


async def test_ordered_parity_across_paths_depths_thresholds_and_visibility(db):
    edges = [
        ("root", "node-10", "supports", .8),
        ("node-10", "node-1", "supports", .7),
        ("root", "", "extends", .9),
        ("", "node-1", "extends", .9),
        ("", "node-1", "supports", .9),
        ("node-1", "root", "supports", .8),
        ("node-1", "a\0b", "extends", .8),
        ("a\0b", "a\0c", "extends", .7),
        ("a\0c", "a\0b", "supports", .9),
        ("root", "hidden", "supports", .9),
        ("hidden", "a\0c", "supports", .9),
        ("root", "expired", "supports", .9),
        ("expired", "a\0c", "supports", .9),
    ]
    await db.executemany(
        "INSERT INTO memory_links (source_id,target_id,link_type,strength,created_at) "
        "VALUES (?,?,?,?,'2026-01-01')", edges,
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id,deprecated,created_at) "
        "VALUES ('hidden',1,'2026-01-01')"
    )
    await db.execute(
        "INSERT INTO memory_metadata (memory_id,invalid_at,created_at) "
        "VALUES ('expired','2000-01-01','2026-01-01')"
    )
    await db.commit()
    for include_deprecated in (False, True):
        # NetworkX serves only the visible projection. Temporarily unhide just
        # deprecation for its oracle; expiry remains enforced in both arms.
        await db.execute("SAVEPOINT oracle_visibility")
        if include_deprecated:
            await db.execute("UPDATE memory_metadata SET deprecated=0")
        oracle = NetworkxGraphStore()
        cases = list(itertools.product(["root", "", "a\0b"], [0, 1, 2, 3, 5], [0, .8, .95]))
        expected = [await oracle.traverse(db, r, max_depth=d, min_strength=s) for r, d, s in cases]
        await db.execute("ROLLBACK TO oracle_visibility")
        await db.execute("RELEASE oracle_visibility")
        actual = [
            await _traverse_cte(db, r, d, s, include_deprecated=include_deprecated)
            for r, d, s in cases
        ]
        assert actual == expected
