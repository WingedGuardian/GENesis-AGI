- **The worktree-removal guard no longer goes silent when a bound stops the
  parse.** It decided by searching `analyze`'s segment list, and a parse halted
  by the substitution-depth or command-length bound yields no segments at all —
  by design, so a searching consumer cannot read "stopped looking" as "nothing
  found". The guard read it as the latter: a removal wrapped past the depth
  bound produced no targets and was allowed, on a guard whose only job is
  blocking.

  It now asks `analyze_checked` once per invocation and treats a blind spot the
  same way it already treats a command shlex cannot tokenize — falling back to
  the coarser regex extractor, which reads the raw text and so cannot be hidden
  from by nesting. Measured by mutation: with the new branch removed, a real
  `git worktree remove` nested eight substitutions deep exits 0 (allowed); with
  it, 2 (blocked). A deep command carrying no removal is still allowed, so the
  fallback discriminates rather than blocking everything unreadable.

  This also restores the chokepoint invariant that every consumer of the shared
  parser either uses the checked entry point or is listed with a stated reason
  why blindness is safe for it. That reason could not be written here.
