- Stop an ambient Git environment from redirecting the review gates' own Git
  queries: the hook launcher now scrubs Git's repository-location variables for
  the hook it runs, not only for its own discovery, and the shared review-state
  and review-scope helpers scrub at every Git call site so a directly-invoked
  gate is covered too. An exported `GIT_DIR`/`GIT_WORK_TREE` previously made
  those helpers describe a different repository than the one under review, in
  every case toward letting a change through.
- Stop a Git attributes setting from quietly lowering how much review a change
  needs. A global `core.attributesFile` marking source files binary collapsed
  the staged diff to nothing countable, and the depth gate then treated a large
  change as a trivial one; the gates now ask Git to ignore that setting when
  they measure a diff.
- Leave your Git configuration alone. Nothing here removes `GIT_CONFIG_GLOBAL`,
  `GIT_CONFIG_SYSTEM`, `GIT_CONFIG_COUNT` or `GIT_CONFIG_PARAMETERS` from the
  environment, because all four are where Git reads `safe.directory` from — and
  on a box where the repository is owned by a different user, such as a
  bind-mounted container or a CI job, removing them made Git refuse and the
  review gates read that refusal as "nothing to review". The push guard also
  needs to see the same configuration as the push it is checking.
