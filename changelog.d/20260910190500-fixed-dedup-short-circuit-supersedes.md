- **Re-sending a correction whose content already existed deprecated nothing.**
  `MemoryStore.store()` returns early when the exact content is already stored,
  and that early return sits about three hundred lines above the supersession
  block -- so a `supersedes` request on that path was dropped without a word.
  It is the likeliest path there is: retrying a correction re-sends the same
  content with a corrected id, and lands exactly there.

  The dedup short-circuit now performs the supersede, with two guards the
  normal path does not need. On that path the successor is not a memory the
  caller chose -- it is whatever the duplicate lookup matched, and that lookup
  consults neither the supersede target nor the deprecation column. So a memory
  can no longer replace itself (which deprecated the only copy and recorded it
  as its own correction), and a correction can no longer land on an
  already-deprecated memory (which deprecated the target toward a successor
  recall filters out -- both halves gone). Both are rejected before anything is
  written.

  The supersede also sits outside the duplicate lookup's own error handler.
  Inside it, a failed supersede was logged as a failed lookup and execution
  fell through into the full store pipeline, writing a second copy of content
  the lookup had just proved already existed.
