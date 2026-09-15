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
- **An archived worktree is no longer readable by other accounts on the
  machine.** A reaped worktree is a verbatim copy of a working tree, which
  routinely holds a private key or environment file. Rolling that into an
  archive under ordinary settings republished it at world-readable permissions
  inside a world-listable directory, so a file that was private in the worktree
  stopped being private the moment it was archived. The archive, the directory,
  and the recovery patch are now created private from the outset rather than
  tightened afterwards.
- **The archive is forced to disk before the original is removed.** Reading a
  freshly written archive back proves the bytes are in memory, not that they
  would survive a power cut, and publishing the finished name guarantees that
  nobody sees a half-written file rather than that the name itself survives a
  crash. A badly timed power loss could therefore keep the deletion and lose the
  archive — the one outcome this whole change exists to prevent.
- **An archived worktree whose name is legal on disk but illegal as a git
  reference now still gets its safety anchor.** Names beginning with a dot, or
  containing a space and several ordinary punctuation characters, were rejected
  when the anchor was created; the archive was still recorded, so the anchor
  that keeps the archived history reachable was silently missing and a later
  cleanup could discard it.
- **Recovering a worktree whose branch was deleted now produces a working
  checkout.** This is the common case rather than an exotic one, because the
  task runner deletes the branch right after cleanup. Recovery previously moved
  the files back, reported success, and left a directory that git refused to
  operate on. It now rebuilds a real checkout at the recorded commit and tells
  you the one command that restores the branch name.
- **A worktree that contains another worktree is left alone.** Moving the outer
  one takes the inner one's files with it and can orphan its history.
- **A cleanup run that cannot list the worktrees now says so instead of
  reporting that there are none.** Failure and emptiness were the same answer,
  so a timed-out or failing scan published a confident, valid-looking view in
  which every worktree had vanished, and nothing downstream could tell that from
  the truth. Such a run now reports the error, changes nothing, and leaves the
  previous view in place.
