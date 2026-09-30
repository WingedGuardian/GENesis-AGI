- **locate no longer returns duplicate hits from linked worktrees outside .claude/worktrees/.**
  The repo scope now prunes every linked worktree that git worktree list --porcelain reports.
