"""Reserve namespaced memory IDs before external writes; runner owns transaction."""


async def up(db):
    await db.execute("""
CREATE TABLE IF NOT EXISTS memory_namespaces (
    peer_id TEXT NOT NULL,
    origin_class TEXT NOT NULL CHECK(origin_class='external_untrusted'),
    collection TEXT NOT NULL,
    content_digest TEXT NOT NULL CHECK(length(content_digest)=64),
    memory_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','complete','deleted')),
    PRIMARY KEY(peer_id,origin_class,collection,content_digest)
)
    """)
