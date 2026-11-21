- **Task creation refuses retained or occupied worktree state rather than resetting it.**
  Live saved-path recovery is unchanged; missing-path recovery may refuse rather
  than attach. This is not full resume durability or a universal Git-GC guarantee.
