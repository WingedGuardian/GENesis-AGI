- **Genesis now records which session is working in which worktree, instead of
  guessing.** With many sessions sharing one repository, nothing said who held
  what, so the daily cleanup could archive a worktree that still had another
  session's unfinished work in it, and one session could edit another's files
  without either noticing. Ownership is now written down when work is happening
  there, using git's own "this worktree is in use" marker — which both the
  cleanup job and git's own removal command already respect, so the protection
  needs no new enforcement anywhere.
- **The signal the previous guards relied on turned out not to exist.** Both of
  them looked for a session whose working directory was inside the worktree, and
  sessions change directory per command, so their working directory never leaves
  the main checkout. Measured on a live install: none of 200 worktrees had a
  session sitting in it, while seven sessions were running. Ownership is now
  keyed on the session's process instead, recorded together with when that
  process started, so a reused process number cannot be mistaken for the original
  owner.
- **Every ownership marker carries a condition under which it lifts**, and a
  daily sweep applies it: the claiming session exiting, the worktree going quiet
  for as long as the cleanup job already waits, uncommitted work being committed
  or discarded. This is deliberate — a marker nobody can decide to remove would
  stop the cleanup job permanently, so one that cannot be given a lifting
  condition is never written in the first place. On the install this was
  developed against the sweep marked 11 of 201 worktrees, leaving the other 190
  untouched.
- **A marker left by something else is reported, never touched.** A hand-written
  one, or one belonging to another tool, is left exactly as found. If such a
  marker is still sitting on a worktree nothing has touched in weeks, the sweep
  says so plainly, because that is the shape a crashed tool leaves behind and it
  would otherwise keep that worktree out of the cleanup job's reach forever.
- **Worktrees created by the optional workflow runner get no special treatment,
  and that is deliberate.** A dedicated rule for them was built and then removed
  after testing it against a real run: the marker stopped that tool's own cleanup
  from finishing, which meant the condition for lifting the marker — that tool
  reporting the work finished — could never become true, and the worktree was
  stuck until someone cleared it by hand. It turned out to protect nothing the
  ordinary "has uncommitted work" rule did not already cover. Installs that do
  not run that tool at all are unaffected either way; it is read only to report
  how many of its environments are active, and a missing or damaged database
  changes nothing.
