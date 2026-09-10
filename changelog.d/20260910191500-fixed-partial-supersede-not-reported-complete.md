- **A supersede that only half-applied was reported as complete.** The two
  steps after the SQLite deprecation -- the vector payload write and the
  `succeeded_by` link -- are best-effort and swallow their own errors, so a
  supersede whose payload write failed left the memory hidden from keyword
  recall and still live in vector recall while the call reported success. The
  failed steps are now named in a `partial` list on the report.

  A supersede that failed OUTRIGHT with an unexpected database error was also
  reported as successful: the error was logged and nothing else, and the
  success report was unconditional. That case is now distinguished from a
  partial and reported as not superseded, because the caller's next move
  depends on whether the old memory is still live.

  The two are kept apart by a boundary at the point of no return. Once the
  deprecation has committed, recall already excludes the old memory, so a
  failure after that point is a partial no matter what threw -- it can never be
  reported as "did not happen at all".
