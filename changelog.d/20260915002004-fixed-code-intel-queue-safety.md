- Code-intelligence requests now use a FULL-sync SQLite queue with atomic
  coalescing, ownership, retry, and terminal-outcome transitions. Existing
  file markers migrate without dropping pending or orphaned work. The runner
  still defers if its execution lock is unavailable, and last-resort disk
  reclamation preserves index caches when their rebuild cannot be queued.
