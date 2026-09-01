"""Create the federation tables — the private Genesis↔Genesis relay (v1).

A cross-owner, cross-install, human-in-the-loop channel between two DIFFERENT
users' Genesis installs. ``federation_contacts`` holds one row per paired peer
(pinned Ed25519 identity key + X25519 encryption key + relay mailbox coordinates
+ a SecretBox-sealed write-cap, NEVER a plaintext cap). ``federation_messages``
is the per-contact, per-direction hash-chained transcript — every message links
to the previous one's ``payload_hash`` so tampering or reordering is detectable.

The bodies (``plaintext``/``ciphertext``) are prunable by the disk-hygiene
retention path; the chain skeleton (``seq``/``prev_hash``/``payload_hash``) is
NEVER deleted, so a pruned transcript still verifies.

Outbound peer messages are held for owner approval (``hitl_state='held'`` +
a linked ``approval_requests`` row) exactly like the WS-8 email autonomy gate
(``pending_email_sends`` + ``email_gate_watcher``); inbound peer content is
stored ``origin_class='external_untrusted'`` so recall wraps it in injection
boundary markers.

Idempotent (``IF NOT EXISTS``). Fresh installs get the same DDL via
``db/schema/_tables.py``; this migration covers existing installs.
"""

from __future__ import annotations

import aiosqlite

_CONTACTS_DDL = """
    CREATE TABLE IF NOT EXISTS federation_contacts (
        contact_id          TEXT PRIMARY KEY,
        display_name        TEXT NOT NULL,
        peer_ed25519_pub    BLOB NOT NULL,          -- pinned identity/verify key (TOFU)
        peer_x25519_pub     BLOB NOT NULL,          -- peer encryption pubkey (Box)
        local_mailbox_id    TEXT NOT NULL,          -- our inbound mailbox on the relay
        peer_mailbox_id     TEXT NOT NULL,          -- peer's inbound mailbox (we write here)
        relay_url           TEXT NOT NULL,
        peer_write_cap_enc  BLOB,                   -- SecretBox-sealed bearer cap (never plaintext)
        state               TEXT NOT NULL DEFAULT 'pending'
                                CHECK (state IN ('pending', 'active', 'revoked')),
        paired_at           TEXT NOT NULL,
        revoked_at          TEXT,
        last_seq_sent       INTEGER NOT NULL DEFAULT 0,
        last_seq_recv       INTEGER NOT NULL DEFAULT 0,
        send_chain_head     TEXT,                   -- last outbound payload_hash
        recv_chain_head     TEXT                    -- last inbound payload_hash
    )
"""

_MESSAGES_DDL = """
    CREATE TABLE IF NOT EXISTS federation_messages (
        msg_id          TEXT PRIMARY KEY,           -- ULID (sortable)
        contact_id      TEXT NOT NULL,              -- FK federation_contacts.contact_id
        direction       TEXT NOT NULL CHECK (direction IN ('in', 'out')),
        seq             INTEGER NOT NULL,           -- per-contact, per-direction monotonic
        prev_hash       TEXT,                       -- previous payload_hash in this chain
        payload_hash    TEXT NOT NULL,              -- H(prev_hash || canonical(payload))
        nonce           BLOB,                       -- Box nonce (prunable)
        ciphertext      BLOB,                       -- prunable
        plaintext       TEXT,                       -- prunable (nulled on prune)
        origin_class    TEXT,                       -- inbound provenance tag (external_untrusted)
        source_pipeline TEXT,                       -- 'peer:<contact_id>'
        hitl_state      TEXT NOT NULL DEFAULT 'proposed'
                            CHECK (hitl_state IN ('proposed', 'held', 'approved',
                                                  'sent', 'rejected', 'quarantined', 'received')),
        approval_id     TEXT,                       -- FK approval_requests.id (outbound holds)
        created_at      TEXT NOT NULL,
        delivered_at    TEXT,
        -- one message per (contact, direction, seq): a duplicate seq would fork
        -- the hash chain. Enforced at the DB layer regardless of app logic.
        UNIQUE (contact_id, direction, seq),
        -- real FK (enforced on PRAGMA foreign_keys=ON connections, i.e. get_db);
        -- the append_message rowcount guard enforces it on FK-off connections too.
        FOREIGN KEY (contact_id) REFERENCES federation_contacts (contact_id)
    )
"""

_INDEX_DDL = (
    "CREATE INDEX IF NOT EXISTS idx_federation_messages_chain "
    "ON federation_messages(contact_id, direction, seq)",
    "CREATE INDEX IF NOT EXISTS idx_federation_messages_hitl ON federation_messages(hitl_state)",
    "CREATE INDEX IF NOT EXISTS idx_federation_contacts_state ON federation_contacts(state)",
)


async def up(db: aiosqlite.Connection) -> None:
    # NOTE: must NOT call db.commit()/BEGIN — the runner owns the transaction.
    await db.execute(_CONTACTS_DDL)
    await db.execute(_MESSAGES_DDL)
    for stmt in _INDEX_DDL:
        await db.execute(stmt)


async def down(db: aiosqlite.Connection) -> None:
    """Drop the federation tables (and indexes) — development/testing only."""
    await db.execute("DROP TABLE IF EXISTS federation_messages")
    await db.execute("DROP TABLE IF EXISTS federation_contacts")
