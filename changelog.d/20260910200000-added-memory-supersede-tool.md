- **Superseding a memory is now its own operation.** Until now the only way to
  mark a memory as corrected was to pass `supersedes` to `memory_store`, which
  meant the deprecation could only happen as a side effect of writing new
  content. There was no way to say "this memory replaces that one" about two
  memories that already existed, and a supersede that failed could not simply be
  retried -- retrying meant re-sending the content too.

  `memory_supersede(old_id, new_id)` does just the one thing. Both ids accept
  the same short handles as the rest of the system, and because the caller names
  both, both can be checked before anything is written: a handle naming no
  memory, an ambiguous prefix, a memory asked to replace itself, or a successor
  that is itself deprecated are all rejected with nothing changed. A failed call
  is a no-op that is safe to retry.

  `memory_store(supersedes=...)` keeps working and is still the right call when
  the correction is new content.
