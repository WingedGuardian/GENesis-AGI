- **Correcting a memory with a short id handle did nothing, silently.**
  `memory_store` accepts a `supersedes` id, and the proactive recall hook prints
  memories as short `id:<8-char>` handles -- but only the READ path
  (`memory_expand`) resolved those handles. On the write path the handle went
  into an exact-match update, matched nothing, and the "did I find it" answer
  was thrown away. The call returned an id, so the caller read success, while
  the stale memory stayed live in recall and the only trace was a `succeeded_by`
  graph edge pointing from something that was not a memory.

  Measured on one install: three such edges inside one 14-minute window, none
  ever before, and every one of the three handles named exactly one memory --
  resolution alone would have prevented all three. Two of them were retried with
  the full id about ninety seconds later, so the silence also duplicated the
  correction; the third was simply lost.

  `supersedes` now accepts the same short handles the rest of the system hands
  out, never guesses an ambiguous one, and refuses a full-length id that names
  no memory instead of writing a dangling edge and reporting nothing. A
  supersede that cannot resolve its target is rejected before anything is
  written, so it can no longer leave half an operation behind.
