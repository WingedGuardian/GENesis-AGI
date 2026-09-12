- **Re-sending a correction whose content already existed deprecated nothing.**
  `MemoryStore.store()` returns early when the exact content is already stored,
  and that early return sits about three hundred lines above the supersession
  block -- so a `supersedes` request on that path was dropped without a word.
  It is the likeliest path there is: retrying a correction re-sends the same
  content with a corrected id, and lands exactly there.

  The duplicate check now runs *after* the supersede target has been resolved,
  so a request to deprecate a memory that does not exist no longer succeeds
  quietly just because the content happened to be a duplicate. When the content
  does already exist, that memory becomes the successor -- and because it is a
  memory the caller never chose, the pair is validated first, using the same
  check `memory_supersede` uses. A memory cannot replace itself, and a
  correction cannot land on an already-deprecated memory that recall filters
  out. Both are rejected before anything is written.
