- **An inbox evaluation that silently skips a URL is now re-queued instead of
  lost.** The old accountability check only caught give-up *language* ("could
  not be fetched"); a response that simply never mentioned one of its URLs
  passed, and the omitted link was permanently absorbed into the evaluated
  baseline — invisible, unrecoverable loss. A per-URL coverage check now
  requires every input URL to leave a trace in the response (verbatim URL,
  slug, or platform mention), the evaluation prompt requires each URL echoed
  as a `**Source:**` line, and a miss re-queues the item through the existing
  bounded retry path with the partial response preserved.

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
