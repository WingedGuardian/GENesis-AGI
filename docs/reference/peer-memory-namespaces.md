# External memory namespaces

`MemoryStore.store_reporting_creation(..., dedup_namespace=DedupNamespace(peer_id,
collection))` reserves an identity for exact UTF-8 content in the tuple
`(peer_id, external_untrusted, collection, content_digest)`. This is an internal
write option, not a peer API or permission to promote remote knowledge. The
knowledge-promotion coordinator must still enforce consent and disclosure.

The file-backed SQLite `memory_namespaces` reservation commits in a short
private transaction before embedding or vector writes. Missing schema,
unavailable storage, conflicting content/provenance, unknown database location,
and an already-open transaction on the store connection refuse the write.
Explicit owner origin, subsystem writes and supersession are incompatible with
this option. Namespaced content bypasses alias normalization and automatic
linking so approved bytes and external origin remain unchanged.

IDs are deterministic SHA256-derived RFC9562 UUIDv8 values. The version marker
is reserved for this path; ordinary writes retain UUIDv4 and ordinary exact
dedup excludes UUIDv8 before applying its candidate limit. Different peers,
origins and collections cannot adopt one another's IDs. The namespace lookup
fails closed, while existing ordinary dedup retains its best-effort behavior.

Reservation status is `pending` until the existing store pipeline completes.
A retry repairs only its deterministic ID and reports `created=False` if the
reservation already existed. Callers performing compensation must respect that
ownership result. Partial vector/FTS/metadata writes never justify deleting or
relabeling another peer's or an owner's record.

Namespaced writes and deletion hold the existing per-ID asyncio lock and a
per-ID kernel file lock under the database's `.memory-namespace-locks` sibling
directory. Cancellation drains an in-flight vector write or deletion before
releasing these locks. Lock files are retained so different processes lock the
same inode; a process exit releases its kernel lock.

Owner deletion records reservation status `deleted` before the existing delete
pipeline touches Qdrant. This state persists even when deletion is deferred,
and rejects stale retries and later same-peer offers of identical content.
Restoration requires a separate explicit owner operation; this slice exposes
no restore command or peer route. This retention policy was chosen by the owner.

SQLite and Qdrant remain separate stores. Existing integrity detection and
repair still owns later vector drift; a complete reservation checks its FTS and
metadata rows, not a fresh vector lookup. This change does not make promotion,
deletion or recall transactional across stores and does not change the existing
integrity-repair policy.
