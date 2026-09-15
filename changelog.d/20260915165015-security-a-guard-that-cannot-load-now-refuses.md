- **A hook guard whose own code fails to load now refuses the command instead of
  disappearing.** Claude Code treats any exit code other than 2 as a non-blocking
  error, so a guard that raised while it was still importing did not degrade — it
  vanished, while the session went on believing it was protected. Six guards are
  covered: protected-path deletion, discard, push/merge, the review gate, the
  worktree guard and the full-suite guard. Each now falls back to a deliberately
  crude read of the raw command text and refuses anything that names a gated
  operation — over-broad by design and loud about it, rather than standing silently
  aside. A payload that names no command at all is refused on the same reasoning,
  and no in-band waiver is honoured on that path, because binding a waiver to the
  command it waives needs the very parser that is missing.

  One guard is deliberately **not** covered: the background-pipe guard, whose only
  usable signal is the pipe character — present in 70% of ordinary commands, so
  refusing on it would make a broken install unrepairable rather than safe. What it
  protects is also the mildest here: a backgrounded pipeline whose output is
  swallowed, which costs a re-run. Two advisory hooks are unaffected, since an
  import failure there loses a note and never a refusal.

  While the hook tree is broken these guards refuse roughly a third of ordinary
  commands. That is the cost of the state, not of normal operation, and the usual
  cause is a partially updated install — which the refusal now says.
