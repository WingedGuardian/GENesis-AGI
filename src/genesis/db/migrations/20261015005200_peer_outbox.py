"""Retained addressed messages; existing SQLite backup owns retention."""


async def up(db):
    from genesis.db.schema._tables import TABLES

    await db.execute(TABLES["peer_outbox"])
