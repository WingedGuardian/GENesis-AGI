- **The inbox now notices when an evaluation silently skips a URL — and says so
  before it starts acting on it.** The accountability check only ever caught
  give-up *language* ("could not be fetched"); a response that simply never
  mentioned one of its URLs passed, and the omitted link was permanently
  absorbed into the evaluated baseline — invisible, unrecoverable loss. A
  coverage check now requires every scanner-recognized input URL's complete
  parsed identity to APPEAR in the response. Scheme and host case plus one leading `www.` are
  presentation variants; userinfo, port, path, query, and fragment remain
  identity-bearing. The evaluation prompt requires each URL echoed as a
  `**Source:**` line. It ships in **shadow**
  (`url_coverage_mode: shadow` in `config/inbox_monitor.yaml`): it computes its
  verdict and logs opaque stable ids for what it *would* have re-queued (never
  raw signed URLs or credentials), and changes nothing. Set
  `url_coverage_mode: enforce` to have a miss re-queue the item through the
  existing bounded retry path with the partial response preserved.

  Coverage now counts only dedicated `**Source:**` fields and preserves legal
  terminal URL characters. Exact retry-exhausted items remain terminal across
  unrelated edits without suppressing genuinely new siblings. New batch rows
  store versioned, lossless item boundaries in the existing database column;
  malformed text or binary values are safely re-derived instead of crashing
  the monitor or replaying a whole file. Legacy rows whose item boundaries
  cannot be reconstructed retain the bounded file-level storm guard.

  Shadow is the default deliberately. The check is new, and replaying it over
  this install's completed evaluations flags roughly half of older responses —
  which would re-queue them into a retry path that parks a whole file after
  `max_retries` with no notification. Watch the shadow log first; enforce when
  the rate reflects responses written under the `**Source:**` prompt.

  **The link the evaluator is asked to fetch is now the link you wrote.** The
  coverage check keeps every trailing character so it can fail closed on
  ambiguous punctuation, which is right for deciding identity and wrong for
  telling the evaluator what to open: a markdown link, a quoted URL or a URL in
  a code span used to be handed over with its wrapper still attached
  (`https://example.com/foo)`), so the fetch asked for a page that does not
  exist. Measured across this install's stored evaluations and current inbox,
  340 of 18,119 extracted links (1.9%) ended in wrapper punctuation; that is
  now 11 (0.06%).

  Trimming is structural rather than a guess at prose: a bracket or quote is
  dropped only when nothing in the link opens it, so a balanced
  `/wiki/Foo_(bar)` and an IPv6 address keep their brackets. Sentence
  punctuation alone is never enough — a link ending in `;` or `!` is left
  exactly as written, because nothing proves that character is prose and
  asking for a different address than you saved is the very loss this check
  exists to catch. That is what the 11 remaining are: 9 links ending in a
  bare comma or semicolon, plus 2 templates the check already skips. Links
  wrapped in markdown emphasis (`**`) are also out of scope and still reach
  the evaluator wrapped.

  The check itself is unchanged and still compares the link as written; it
  simply also accepts the trimmed form **of that same link**, so an answer
  citing exactly what it was asked for counts. A different link that merely
  looks similar still does not.

  One known false positive, left documented rather than patched: a response that
  cites a URL *without* its tracking or query parameters (the prompt asks for
  verbatim) reads as a miss. Stripping tracking params does not fix it — measured
  at 0 of 146 rescued — and stripping the whole query string would merge
  genuinely different URLs (`watch?v=A` and `watch?v=B`), which is worse than the
  false positive.

- **A note written directly above a URL now travels with it.** The natural way
  to annotate a saved link — intent on one line, URL on the next — used to be
  split into two unrelated items, so the evaluator never saw why the link was
  saved. Adjacent prose now rides in the URL's own item (a blank line keeps a
  thought independent), and an already-evaluated line elided from a delta
  leaves a separator so unrelated lines can't be mistaken for annotations.

- **Inbox items are now evaluated one at a time by default**
  (`items_per_eval: 1`, previously 5), with default effort `high` and a 20-minute
  evaluation timeout. Each link gets an evaluation session to itself — no more
  depth dilution across batch neighbours — at the cost of more (smaller)
  sessions. Raise `items_per_eval` in `config/inbox_monitor.yaml` to trade
  depth for fewer sessions.

- **Editing an inbox file before its previous snapshot was evaluated no longer
  burns a retry or strands sibling batches.** Startup recovery atomically
  returns every pre-dispatch row to the bounded retry lane without incrementing
  its retry count. Complete outstanding work is rebuilt from the current file
  and completed baseline, including a never-created tail when a crash interrupted
  multi-batch row creation.

- **An inbox evaluation is not baselined before its synchronous durable side
  effects finish.** Follow-up and build-lane writes still treat ordinary local
  errors as non-fatal, but cancellation or process exit leaves the batch
  non-completed and recoverable instead of permanently hiding a missing side
  effect behind a completed baseline.
