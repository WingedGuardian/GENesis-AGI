- Recovering an archived worktree with `worktree_lifecycle.py --recover` now
  brings back its uncommitted edits to tracked files: modifications, deletions
  and staged new files. The reaper always saved them as `.dirty.patch`, but
  recovery never applied the patch and left it in the tree as a stray untracked
  file. It is now applied with `git apply --3way`, and the reapplied changes
  come back staged. Archives made before this change are covered too. If the
  branch has moved on and the edits conflict, the worktree is left at its clean
  checkout, the patch is kept in it as `.dirty.patch`, and a loud message says
  the edits were not reapplied. Separately, a worktree whose branch has no
  commits of its own is no longer archived on the short 7-day "merged" clock
  while it holds uncommitted work. Such a branch has merged nothing, and it had
  only passed the merge test vacuously. It now waits the 14-day unmerged window
  and shows as at-risk in the meantime. A clean one still drains at 7 days.
