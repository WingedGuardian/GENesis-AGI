"""Pre-write-boundary campaign names are normalized at init, before the
scheduler registers any job.

``create_campaign``/``update_campaign`` strip control characters at the write
boundary, so nothing new lands dirty. Rows written before that shipped were never
healed — the migration that would have done it (``d0012``) was reverted because
data migrations run AFTER ``_init_campaigns`` has already registered each
campaign's APScheduler job as ``campaign_{name}``, so renaming there orphans the
live job. Healing inside campaign init, before ``runner.start()``, is what makes
this safe.

The rows are written with raw SQL here specifically to BYPASS the write-boundary
strip — that is the only way to reproduce a pre-fix row.
"""

from __future__ import annotations


async def _insert_raw(db, cid: str, name: str) -> None:
    """Insert a campaign bypassing crud (and therefore the write-boundary strip)."""
    await db.execute(
        "INSERT INTO campaigns (id, name, strategy_doc_path, cron_cadence, "
        "created_at, status) VALUES (?, ?, ?, ?, ?, ?)",
        (cid, name, "/tmp/s.md", "0 */8 * * *", "2026-06-07T00:00:00Z", "active"),
    )
    await db.commit()


async def test_dirty_name_is_normalized(db):
    from genesis.campaigns.name_heal import heal_campaign_names
    from genesis.db.crud import campaigns as crud

    await _insert_raw(db, "c-dirty", "weekly\ndigest")
    assert await heal_campaign_names(db) == 1
    assert await crud.get_campaign_by_name(db, "weekly digest") is not None


async def test_bidi_mark_in_name_is_normalized(db):
    """U+061C was uncovered by the old hand-enumerated set."""
    from genesis.campaigns.name_heal import heal_campaign_names
    from genesis.db.crud import campaigns as crud

    await _insert_raw(db, "c-alm", "out" + chr(0x061C) + "reach")
    assert await heal_campaign_names(db) == 1
    assert await crud.get_campaign_by_name(db, "out reach") is not None


async def test_clean_names_are_a_no_op(db):
    from genesis.campaigns.name_heal import heal_campaign_names
    from genesis.db.crud import campaigns as crud

    await _insert_raw(db, "c-clean", "upstream-pr-steward")
    assert await heal_campaign_names(db) == 0
    assert await crud.get_campaign_by_name(db, "upstream-pr-steward") is not None


async def test_emoji_name_survives_the_heal(db):
    """ZWJ sequences are Cf but load-bearing — the heal must not mangle them."""
    from genesis.campaigns.name_heal import heal_campaign_names
    from genesis.db.crud import campaigns as crud

    name = "launch \U0001f468‍\U0001f4bb crew"
    await _insert_raw(db, "c-emoji", name)
    assert await heal_campaign_names(db) == 0
    assert await crud.get_campaign_by_name(db, name) is not None


async def test_collision_leaves_the_row_untouched(db):
    """``campaigns.name`` is UNIQUE — two dirty names normalizing to the same
    clean name must not merge two campaigns' identities."""
    from genesis.campaigns.name_heal import heal_campaign_names
    from genesis.db.crud import campaigns as crud

    await _insert_raw(db, "c-a", "alpha beta")           # already clean
    await _insert_raw(db, "c-b", "alpha\nbeta")          # normalizes onto c-a
    assert await heal_campaign_names(db) == 0
    rows = {r["id"]: r["name"] for r in await crud.list_campaigns(db)}
    assert rows["c-a"] == "alpha beta"
    assert rows["c-b"] == "alpha\nbeta", "colliding row must be left as-is"


async def test_heal_is_idempotent(db):
    from genesis.campaigns.name_heal import heal_campaign_names

    await _insert_raw(db, "c-idem", "a\tb")
    assert await heal_campaign_names(db) == 1
    assert await heal_campaign_names(db) == 0


async def test_heal_runs_before_runner_start():
    """The ordering IS the fix — assert it structurally so a refactor that moves
    the heal after runner.start() (recreating the d0012 desync) fails here."""
    import inspect

    from genesis.runtime.init import campaigns as init_campaigns

    src = inspect.getsource(init_campaigns.init)
    assert "heal_campaign_names" in src
    # Anchor on the awaited CALLS, not the bare names: the explanatory comment
    # above the heal mentions ``runner.start()`` in prose, and matching that
    # would compare against the comment's position rather than the call's.
    assert src.index("await heal_campaign_names(") < src.index("await runner.start()"), (
        "name heal must run BEFORE runner.start() — otherwise the scheduler "
        "registers job ids from un-normalized names (the reverted d0012 bug)"
    )


async def test_name_that_normalizes_to_empty_is_left_as_is(db):
    """A legacy name that is ENTIRELY control characters must not be blanked.

    ``campaigns.name`` is NOT NULL and the scheduler derives its job id from the
    name, so writing an empty name would be worse than leaving the row dirty.
    (The reverted d0012 migration covered this case; keep the coverage.)
    """
    from genesis.campaigns.name_heal import heal_campaign_names
    from genesis.db.crud import campaigns as crud

    await _insert_raw(db, "c-empty", "\n\t\n")
    assert await heal_campaign_names(db) == 0
    rows = {r["id"]: r["name"] for r in await crud.list_campaigns(db)}
    assert rows["c-empty"] == "\n\t\n", "row must survive unchanged, not be blanked"


async def test_whitespace_only_name_is_left_as_is(db):
    """Same guard via the .strip() side effect rather than the control-char sub."""
    from genesis.campaigns.name_heal import heal_campaign_names
    from genesis.db.crud import campaigns as crud

    await _insert_raw(db, "c-ws", "   ")
    assert await heal_campaign_names(db) == 0
    rows = {r["id"]: r["name"] for r in await crud.list_campaigns(db)}
    assert rows["c-ws"] == "   "
