"""Record native peer public-state transitions without a second task store."""

from genesis.db.peer_status import install


async def up(db):
    row = await (await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cc_sessions'"
    )).fetchone()
    if row is None:
        return  # Pre-base path: canonical create_all_tables installs the projection.
    await install(db)
