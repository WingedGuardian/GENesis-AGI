- **The dashboard now shows what the worktree cleanup is about to do, before it
  does it.** Every worktree appears with a single plain state — in use,
  protected, fresh, at risk, or ready to be cleaned up — and the reason behind
  it. Previously the only way to know was to read the cleanup job's log after the
  fact, or run it by hand and interpret the output.
- **The view and the cleanup cannot disagree**, because the view does not compute
  anything: it renders the cleanup's own classification, produced by the same
  code path that acts on it. A worktree can never be described one way and
  treated another.
- **The session-start summary of unfinished work now reads that same
  classification** instead of measuring worktree activity itself, so the two
  places that talk about stale work give the same answer.
- Classifications produced without network access are shown but never saved as
  the shared view, because that check can only ever downgrade a finished branch
  to unfinished — harmless for whoever asked for it, misleading for everyone
  else reading the same screen later.
