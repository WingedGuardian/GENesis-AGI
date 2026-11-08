- Queued indexing preserves separate Codebase and GitNexus requests, retry debt,
  and claim/outcome history, including original failure evidence across upgrades.
  An existing queue is not converted on update: until an explicit cutover,
  new requests are kept in the format the previous version reads.
