- **The inbox now notices when an evaluation silently skips a URL — and says so
  before it starts acting on it.** The accountability check only ever caught
  give-up *language* ("could not be fetched"); a response that simply never
  mentioned one of its URLs passed, and the omitted link was permanently
  absorbed into the evaluated baseline — invisible, unrecoverable loss. A
  coverage check now requires every input URL to APPEAR in the response
  (scheme- and `www.`-insensitively), and the evaluation prompt requires each
  URL echoed as a `**Source:**` line. It ships in **shadow**
  (`url_coverage_mode: shadow` in `config/inbox_monitor.yaml`): it computes its
  verdict and logs what it *would* have re-queued, and changes nothing. Set
  `url_coverage_mode: enforce` to have a miss re-queue the item through the
  existing bounded retry path with the partial response preserved.

  Shadow is the default deliberately. The check is new, and replaying it over
  this install's completed evaluations flags roughly half of older responses —
  which would re-queue them into a retry path that parks a whole file after
  `max_retries` with no notification. Watch the shadow log first; enforce when
  the rate reflects responses written under the `**Source:**` prompt.

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
  burns a retry.** Supersession is bookkeeping, not a failure; repeatedly
  edited files no longer walk toward the permanent-failure cap.
