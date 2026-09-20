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
  they measure a diff. The variables naming your own Git config files are left
  alone in both directions — emptying them would take `safe.directory` and your
  credential helper with them, and the push guard has to see the same
  configuration as the push it is checking.
