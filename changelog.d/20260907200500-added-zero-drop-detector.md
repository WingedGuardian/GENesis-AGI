- **Genesis now enumerates its own stranded work instead of trying to remember
  it.** Finished code sitting on a branch nobody pushed, a pushed branch with no
  pull request, edits left uncommitted in a worktree — all of it was invisible
  unless somebody happened to think of it. A detector now sweeps the repository
  on a schedule and keeps one row per standing condition, so the question "what
  fell through the cracks?" has an answer that was counted rather than recalled.

  Getting that count right is the whole problem. Because this project
  squash-merges, a branch that landed weeks ago still looks like it is ahead of
  the main line forever, so simply asking git turned up 145 candidates of which
  roughly 18 were real. Matching each branch against the project's pull-request
  history by NAME is what recovers the precision — but a name is a hint, not a
  receipt, and treating it as one hid real work. A pull request records the
  exact commit it merged, and the server can be asked what it actually holds;
  the detector now prefers those. If the recorded commit IS your local commit,
  or contains it, the work landed and no clock is consulted. If the server's
  copy of the branch differs from yours and yours is not simply older, then you
  are holding commits that exist nowhere else, and no pull request on that name
  covers them — however recently it merged, and whether it merged or was
  abandoned. Only when the commits themselves settle nothing do the merge and
  close timestamps get a say. Measured against this project's history, the
  older name-and-clock rule was hiding five branches whose work existed on no
  server, including finished fixes with tests; four of them sat behind a closed
  pull request and one behind an open one that was reviewing something else.

  Coverage that cannot be established is now reported rather than assumed. If
  the merged commit is one this machine has never fetched, the branch is
  flagged as unconfirmed and carries the single command that settles it, on the
  principle that an extra row somebody dismisses costs less than a clean board
  that lied. Where the ambiguity runs the other way — the server's copy differs
  and there is no way to tell ahead from behind — the detector reports neither,
  leaving any existing row exactly as it was. Closed-but-unmerged pull requests
  are still read as deliberate abandonment and suppressed, but a decision to
  abandon covers the work that was IN the pull request, not commits made
  afterwards or never sent. Every run publishes its full arithmetic, including
  what it declined to judge.

  There is no list of branch-name prefixes to ignore. A branch that is meant to
  sit there — a backup, a scratch experiment — is acknowledged with a written
  reason, which leaves a record a person can read instead of a rule nobody can
  see. The acknowledgement is tied to the exact commit it was granted against,
  so it lapses by itself the moment new work lands on that branch — and only
  then. An earlier version let ordinary typing revoke one: editing a file
  inside an acknowledged working copy briefly pushed it under an age filter,
  and the reconciler read "not reported this time" as "gone". Not reporting
  something and it having gone away are now different things, which is also
  what stops a recurring finding from silently restarting its count.

  Three failure modes shaped the rest. A sweep that cannot see everything says
  so and changes nothing, rather than reporting the branches it failed to look
  at as resolved — a detector that manufactures a clean board is worse than no
  detector. A detector that quietly dies would go on answering "nothing"
  forever, so it reports its own liveness. And the subtler one: a detector that
  is alive but half-blind — say its access token expired — would keep its
  liveness green, keep its numbers, and never notice anything new again. That
  now raises its own alarm, separately from whatever it found, and every count
  it publishes says which parts of the picture the last look actually covered.

  Read the board with `zero_drop_status`, which reports how long ago it was
  compiled, because a stale zero is not a clean one. Off, quiet, or noisy is a
  setting (`zero_drop`: `off`, `observe`, `alert`). It ships on `observe`: the
  board fills and is readable, while the summary-alert lane waits on a
  refinement to how such alerts expire. A damaged setting falls back to
  `observe` rather than to `off`, on the principle that a silent detector is
  the more dangerous of the two.
