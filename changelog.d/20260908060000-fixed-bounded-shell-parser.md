- **A crafted Bash command could run a safety guard past its timeout, and a timed-out
  hook does not block — it permits.** The shared shell parser had no bound on the work
  a single command could ask of it. Cost rises with the length of the command, with
  its nesting depth, and (measured, 3.4x) with the shape of its padding, and Claude
  Code's documented hook contract is explicit that a hook which reaches its timeout
  "doesn't block the tool call". So a guard that ran out of clock did not refuse the
  command — it allowed it.

  Measured end to end against the real protected-paths guard, on a payload it
  genuinely refuses, under its registered 10s timeout: refused in under half a second
  unnested at 64 KB, but **killed at the timeout** at deep nesting, at every depth
  once the command reached 300 KB, and — with no nesting whatsoever — at 450 KB.
  Eleven of fifteen measured cells failed open. Reaching this needs a command far
  larger than ordinary work produces, which for a security guard is the threat model
  rather than a mitigation.

  Both axes are now bounded, because neither subsumes the other: a length cap alone
  leaves nesting free to multiply the work, and a depth bound alone leaves the first
  pass — which happens before any recursion — completely unbounded. Every one of the
  same fifteen cells now refuses, well inside the budget. Both limits are set from
  data rather than taste, sized against the longest and deepest commands in this
  install's own history, and neither fires on any of them; no command's top-level
  segments change, which is what keeps the guards that match working directories by
  exact string unaffected.

  The limits are sized against the tightest path rather than the loosest, which is not
  the one the individual guards are registered on: the shell safety hook is registered
  at 5 seconds and delegates to three guards over the same command, one of which
  analysed it three times over — five parses on one budget, not the two an earlier
  measurement assumed. Those duplicates are now parsed once. The two limits are chosen
  together, because the parser re-scans the remaining text at every level and the
  worst case is a command at the size limit nested to the depth limit — cost is length
  times levels, so neither number means anything alone. The two directions are
  deliberately not treated as symmetric: over the size limit a command is refused or
  prompted, while over the timeout it runs unchecked, so margin is bought on the side
  that fails open.

  Bounding the work alone would have traded one fail-open for a worse one — a guard
  that quietly stops seeing a buried command and allows it — so the bounds and the
  signal ship together. One traversal now returns both what the command runs and
  whether the parser could read all of it, and every guard that fails closed on an
  unreadable command asks that single question. A test derives the consumer set from
  the code and requires it, so a consumer added later cannot quietly go back to the
  unchecked call; an over-length command yields no segments at all rather than a
  parsed prefix, because truncating a command mid-string changes the meaning of
  everything after the cut.

  Refusal messages also stopped guessing at the cause. Each blind spot carries its own
  explanation and its own way out, so a command that is merely too long or too deeply
  nested no longer tells the reader to go fix quoting that was never the problem — and
  the protected-paths fallback no longer describes an unresolved shell variable or an
  unresolvable relative path as "an untokenizable command", which it had been doing
  for two of its three callers.
