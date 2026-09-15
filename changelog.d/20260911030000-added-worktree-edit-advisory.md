- **A session is now told when it edits inside a worktree another live session is
  working in.** It is a note, never a refusal: the edit goes through, and the
  message says who holds the worktree and suggests checking with them or moving
  to your own. This is the case that had no guard at all — the existing
  protections only ever covered deleting a worktree, not writing into one, and it
  came within one instruction of happening to a worktree holding more than a
  hundred lines of another session's unsaved work.
- **Ownership is claimed on the first edit into a worktree, not only when one is
  created.** Two hundred worktrees already exist and sessions mostly pick up an
  existing one rather than make a new one, so claiming only at creation would
  leave the common case unclaimed — and a warning that can never fire is worse
  than none, because it reads as coverage. Nothing is required of you; the claim
  is taken and released automatically.
- **The note is delivered on the only channel that reaches the session.** An
  earlier version of this wrote it to the error stream, which for this kind of
  hook goes to a debug log nobody reads — so the warning existed and never
  arrived. It was checked by capturing that output directly, which proves
  something was produced and nothing about whether anyone receives it. The two
  are not the same test, and only the second one matters.
- **A bug in this advisory can no longer refuse an edit.** It was wired to a
  helper that turns any unexpected error into a refusal, which is right for the
  guards that protect irreversible actions and wrong here — that helper's own
  documentation says so. It now fails open: a failure is reported and the edit
  proceeds.
- **A correction to what the older guard's own description promised.** It
  advertised blocking a deletion when another session had that worktree as its
  working directory. The code does check that, but the check cannot fire in
  practice — sessions change directory per command, so their working directory
  never leaves the main checkout, measured as none of 200 worktrees on a live
  install. What actually makes deletion safe there is the blanket refusal below
  it. The description now says so, because believing otherwise is how two guards
  came to be built on a signal that was never present.
- **Claims no longer reach outside this project.** Ownership was worked out from
  where a file sits on disk, which cannot tell one project's worktree from
  another's — so a session working here, handed a path inside an unrelated
  checkout, would have written this project's lock into that other project,
  where it would pin a worktree the owner's own tools then refuse to remove.
  Ownership is now decided by which repository a worktree actually belongs to,
  and anything outside this one is left alone. Where the question cannot be
  answered, the answer is "not ours": a missed note costs a warning, a wrong
  claim costs someone else's repository.
