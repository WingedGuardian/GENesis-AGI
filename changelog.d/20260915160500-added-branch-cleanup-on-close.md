- **A pull request's branch is now deleted when the request closes without
  merging.** Merged requests already cleaned up after themselves; closed ones
  did not, and every abandoned or superseded request stranded its branch on the
  public repository. Measured before this change: thirty of the hundred-odd
  branches on the remote were exactly such leftovers, and because a squash
  merge severs ancestry, those leftovers are indistinguishable from unmerged
  work — which is how stale branches were later mistaken for pending changes
  and grew duplicate pull requests for work that had already landed. Deletion
  is recoverable (the closed request keeps a restore button), skips branches
  another open request still uses, and can be switched off in the Actions UI.
