- **Cleaning up an old worktree no longer destroys anything.** The daily cleanup
  used to move a worktree aside and then permanently delete it a week later, and
  restoring one deliberately did not bring back edits that had never been
  committed — so a worktree holding unsaved work could be cleaned up and, seven
  days on, be gone for good. It is now packed into a verified archive that is
  kept indefinitely, with a patch of any uncommitted edits saved beside it. On
  the install this was developed against, the very next cleanup would have taken
  twenty-five worktrees, two of which held uncommitted work.
- **The archive is only trusted once it has been read back in full.** Writing it
  and checking the first entry is not a check: a half-written archive passes that
  and then the original is deleted. Every entry is now walked and the count
  compared against the source before anything is removed, and if any part of that
  fails the original is left exactly where it was.
- **Worktrees that are still being worked in are left alone**, including ones with
  a paused rebase or merge, ones containing another worktree, and ones explicitly
  marked as in use. Each worktree now also gets a single plain-language state —
  in use, protected, fresh, at risk, or ready to clean up — so what the cleanup
  decided and what any other view reports can never disagree.
- **Asking for the machine-readable report now returns only that.** When the
  report was requested without network access, an explanatory note was printed
  ahead of the data on the same channel, which made the output unparseable for
  anything reading it programmatically. The note now goes where a person reads it
  and a parser does not.
