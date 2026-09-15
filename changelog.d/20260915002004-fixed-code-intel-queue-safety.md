- Code-intelligence requests now use a FULL-sync SQLite queue with atomic
  coalescing, ownership, retry, and terminal-outcome transitions. Existing
  file markers are atomically claimed during migration so rolling-upgrade
  writers cannot be unlinked, and lock-contended enqueues use a durable spool
  instead of disappearing. The runner stops and restores work when its
  escalation query errors, still defers if its execution lock is unavailable,
  and only clears last-resort index caches after their rebuild is durably queued.
