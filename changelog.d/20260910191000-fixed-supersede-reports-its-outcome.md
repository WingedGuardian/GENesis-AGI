- **A supersede that could not be performed now says so to the caller.** Until
  now `memory_store` returned a memory id whether or not the deprecation it was
  asked for had happened: a failure was written to the server log and nothing
  else, so a session was told its correction had landed while the stale memory
  stayed live in recall.

  When `supersedes` is passed, the call now returns a report -- whether the
  deprecation happened, which id did land, why it did not, and which ids
  collided for an ambiguous handle. Without `supersedes` the return is the
  memory id string exactly as before, so the overwhelming majority of calls are
  unchanged.

  It reports rather than raises on purpose: the memory is durable by that
  point, and an exception reads as "the store failed" and invites the retry
  that duplicates the memory. The advice fits the reason -- "re-send with a
  full 36-character id" is the fix for a handle that named nothing, and the
  wrong instruction for a correction that collided with its own target, where
  re-sending reproduces the collision. An empty or all-whitespace `supersedes`
  is treated as no request at all rather than as a supersede that succeeded.
