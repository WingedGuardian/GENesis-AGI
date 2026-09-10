- **A supersede that did not happen now says so.** `memory_store` returned a
  memory id whether or not the deprecation it was asked for had taken place: a
  failure went to the server log and nowhere else, so a session was told its
  correction had landed while the stale memory stayed live in recall.

  When `supersedes` is passed, the call now reports whether the deprecation
  happened. Without it, the return is the memory id string exactly as before, so
  the overwhelming majority of calls are unchanged.

  There is only one outcome that needs reporting rather than raising: the memory
  was stored and the deprecation then failed on something unexpected. Everything
  else -- a handle naming no memory, an ambiguous one, a memory asked to replace
  itself, a successor that is already deprecated -- is checked before anything is
  written, so it simply fails with nothing left behind. And the remedy for the
  one partial case is now a call the caller can actually make:
  `memory_supersede(old, new)` finishes the job without re-sending the content.
  An empty or all-whitespace `supersedes` is treated as no request at all rather
  than as a supersede that succeeded.
