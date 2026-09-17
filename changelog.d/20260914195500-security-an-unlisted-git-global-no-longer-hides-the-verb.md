- **An unlisted git global option no longer hides the subcommand from the push
  guard.** Verb discovery walks a git command's leading options to find the
  subcommand, and stepped over any option it did not recognise as consuming a
  value — assuming an unknown option to be harmless. When such an option does
  consume one, that VALUE lands in the verb slot: the walk reports it as the
  subcommand, the real operation is never seen, and gates keyed on that
  subcommand stand down. Three real options behaved exactly this way and were
  listed one at a time as each was found.

  The failure direction is now inverted, on the signal that means "the verb
  cannot be trusted". An option in none of the three classification sets marks
  the verb UNESTABLISHED, which routes to the unresolved-verb blind spot the
  push guard already refuses. Previously the walk answered confidently and
  wrongly; it now declines to answer. A future git release adding a
  value-consuming global becomes a refused command rather than an open door.

  The three sets were MEASURED against the installed binary with an oracle
  rather than read from help output, which omits several of these options
  entirely. Of 750 candidates from the binary's own strings table: 8 consume a
  value, 12 are valueless, 4 run no subcommand, 726 are rejected outright.

  **What did NOT change is the accessor that answers "which word is the verb".**
  An earlier revision made that return "unknown" too, which is the honest
  answer and was wrong to ship: 24 call sites across five guards read its
  existing sentinel as "this segment is not the operation I gate", so widening
  it widened what each guard ignores. Measured on that revision, a worktree
  removal behind `--bare` went from refused to allowed, and a `--no-verify`
  behind any unclassified global became invisible to the commit rule — a
  fail-open introduced by a change meant to close one. Teaching each guard to
  consult the blind spot is per-guard work on five enforcement hooks and is
  tracked separately; this change does not attempt it, and the residual it
  leaves is exactly what was there before.

  Measured over 50,845 unique real commands (18,936 containing git/gh segments,
  35,311 segments) by diffing every parser answer a guard can branch on:
  `git_subcommand`, `gh_pr_subcommand`, `commit_skips_hooks` and the subcommand
  index are **identical on both trees, 0 flips**. Only the trust signal moves,
  on 2 commands: an unknown-option probe that git itself rejects, now refused
  instead of parsed, and `git --help <topic>`, which stops being flagged
  because a help query runs no subcommand.

- **A deliberate relaxation, and the check that keeps it honest.** A gated verb
  written after one of the options that run no subcommand — `--exec-path` and
  its siblings — was refused and is now allowed, because git never
  reaches the verb. That is a false block removed, but it makes several allow
  decisions depend on a classification — so both new sets are re-derived from
  the installed binary on every CI run, and the checks fail if an exempt option
  ever starts running the subcommand, or a supposedly valueless one turns out
  to consume a value. A fix for a hand-maintained list that fails open should
  not introduce a second one.

  The oracle for that derivation must not resolve through the repository.
  Reading the marker from the probe repo's own config makes the lookup miss for
  any global that changes repository or config resolution, which scores it as
  "git rejects this" while its subcommand runs perfectly well — how `--bare`
  came to be absent from every set while the sweep reported a tidy total. The
  marker now lives in a file named by absolute path. A completeness check
  seeded from git's own usage line covers the other direction, since a sweep
  blind to a class reports the same tidy total either way.

- **Three spellings of the same `gh` option now answer alike.** A glued short
  value (`-R<value>`) was tested as a whole token, so a repository passed that
  way was read as an unreadable option NAME and flagged, while the separated
  and `=`-attached spellings of the identical command were not. Glued values
  are recognised for the shorts a dispatcher declares — measured per program,
  because `gh` accepts `-Rowner/repo` and `git` rejects `-C/path` outright.

- A refusal names a remedy that applies. Two causes now reach one predicate,
  and telling a session to write its subcommand out literally when it already
  is names a rewrite it cannot perform — on a hard block, the message is the
  only way out.
