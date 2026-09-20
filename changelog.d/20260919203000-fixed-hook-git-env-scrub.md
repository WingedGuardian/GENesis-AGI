- Stop an ambient Git environment from redirecting the review gates' own Git
  queries: the hook launcher now scrubs Git's repository-location variables for
  the hook it runs, not only for its own discovery, and the shared review-state
  and review-scope helpers scrub at every Git call site so a directly-invoked
  gate is covered too. An exported `GIT_DIR`/`GIT_WORK_TREE` previously made
  those helpers describe a different repository than the one under review, in
  every case toward letting a change through.
