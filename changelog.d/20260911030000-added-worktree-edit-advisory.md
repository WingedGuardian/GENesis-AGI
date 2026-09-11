- **A session is now told when it edits inside a worktree another live session
  is working in.** It is a note, never a refusal: the edit goes through, and the
  message says who holds the worktree and suggests checking with them or moving
  to your own. This is the case that has no other guard at all — the existing
  protections only ever covered deleting a worktree, not writing into one, and
  it came within one instruction of happening to a worktree holding more than a
  hundred lines of another session's unsaved work.
- **Ownership is claimed on the first edit into a worktree, not only when one is
  created.** Two hundred worktrees already exist and sessions mostly pick up an
  existing one rather than make a new one, so claiming only at creation would
  leave the common case unclaimed — and a warning that can never fire is worse
  than none, because it reads as coverage. Nothing is required of you; the claim
  is taken and released automatically.
- **A correction to what the older guard's own description promised.** It
  advertised blocking a deletion when another session had that worktree as its
  working directory. The code does check that, but the check cannot fire in
  practice — sessions change directory per command, so their working directory
  never leaves the main checkout, measured as none of 200 worktrees on a live
  install. What actually makes deletion safe there is the blanket refusal below
  it. The description now says so, because believing otherwise is how two guards
  came to be built on a signal that was never present.
