- **Correcting a memory could silently not happen.** `memory_store` accepts a
  `supersedes` id, and the proactive recall hook prints memories as short
  `id:<8-char>` handles -- but only the READ path (`memory_expand`) resolved
  those handles. On the write path the handle went into an exact-match update,
  matched nothing, and the "did I find it" answer was thrown away. The call
  returned an id, so the caller read success, while the stale memory stayed
  live in recall and the only trace was a `succeeded_by` graph edge pointing
  from something that was not a memory.

  Measured on this install: three such edges inside one 14-minute window, none
  ever before; two of the three were retried with the full id about ninety
  seconds later, so the silence also duplicated the correction, and the third
  was simply lost.

  `supersedes` now accepts the same short handles the rest of the system hands
  out and never guesses an ambiguous one. When it cannot resolve the target the
  memory is still stored, and the call says plainly that the supersede did not
  happen, which id did land, and how to complete it. A supersede that only
  half-applies -- the SQLite deprecation lands but the vector payload does not,
  which would hide the memory from keyword recall while leaving it live in
  vector recall -- now reports that too, instead of claiming success. And
  re-sending a correction whose content already exists still deprecates its
  target: that path returned early, so the retry above would have reported a
  supersede it never performed, and an unresolvable id on that same path would
  have stored a second copy of content that already existed.

  Two further ways the same lie could arrive, both closed. The successor of a
  supersede is now validated on the path where the caller did not choose it: a
  memory cannot replace itself (re-sending unchanged content alongside a handle
  for the memory holding it used to deprecate the only copy and record it as its
  own correction), and a correction cannot land on an already-deprecated memory
  (which would deprecate the target toward a successor recall filters out --
  both halves gone, reported as done). And a supersede that fails outright with
  an unexpected database error is now reported as failed rather than logged and
  forgotten, with advice that fits the reason instead of a retry instruction
  that cannot apply.

  A second review round closed three more ways the report could mislead. A
  failure that happens *after* the deprecation has already committed is now
  reported as a partial rather than a total failure, so the caller is no longer
  told the old memory is probably still live when recall already hides it. An
  empty `supersedes` value is treated as no request at all, instead of producing
  a success report for an operation that never ran. And alias normalization now
  runs before the duplicate check rather than after it -- previously a memory
  containing a default alias was stored in canonical form while the next save of
  the same raw text searched for the raw form, missed the row it had just
  written, and stored a second copy of identical content, which also made the
  "re-send and it will not be duplicated" instruction untrue for that text.
